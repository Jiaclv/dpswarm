import copy
import importlib.util
import json
from pathlib import Path
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


PATH = Path(__file__).resolve().parents[1] / "transport.py"
spec = importlib.util.spec_from_file_location("team_v2_transport_tested", PATH)
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)
TOOLS = [{"name": "write", "parameters": {"type": "object", "properties": {"content": {"type": "string"}},
                                          "required": ["content"], "additionalProperties": False}}]


def raw_reply(model="glm-5.3", content="print('ok')", arguments=None, usage=True):
    message = {"role": "assistant", "content": "Applying this action.", "reasoning_content": "fixture reasoning",
               "tool_calls": [{"id": "a", "type": "function", "function": {
                   "name": "write", "arguments": json.dumps({"content": content}) if arguments is None else arguments}}]}
    body = {"model": model, "choices": [{"finish_reason": "tool_calls", "message": message}]}
    if usage:
        body["usage"] = {"prompt_tokens": 130, "completion_tokens": 40, "total_tokens": 170,
                         "prompt_tokens_details": {"cached_tokens": 100},
                         "completion_tokens_details": {"reasoning_tokens": 30}}
    return {"http_status": 200, "raw": json.dumps(body)}, message


@pytest.fixture
def setup_transport(tmp_path, monkeypatch):
    monkeypatch.setattr(transport.original, "_keyconfig", lambda name: {
        "GLM_API_KEY": "sk-fixture-credential-12345", "GLM_BASE_URL": "https://fixture.invalid/coding/v1"}[name])
    return transport.V2Transport(tmp_path)


def complete(client, messages=None, model="glm-5.3"):
    return client.complete(model, messages or [{"role": "system", "content": "fixture policy"}],
                           tools=TOOLS, run_id="fixture-run", role="executor", task_id="fixture-task")


def test_native_wire_and_usage_preserved_without_private_history_cache(setup_transport, monkeypatch):
    reply, assistant = raw_reply()
    seen = []
    def exchange(endpoint, payload, key, **limits):
        seen.append((json.loads(payload), limits))
        return reply
    monkeypatch.setattr(transport, "_http_exchange", exchange)
    messages = [{"role": "system", "content": "No hidden string suffix required."},
                {"role": "assistant", "content": None, "reasoning_content": "earlier fixture",
                 "tool_calls": [{"id": "old", "type": "function", "function": {"name": "write", "arguments": '{"content":"x"}'}}]},
                {"role": "tool", "tool_call_id": "old", "content": "ok"}]
    before = copy.deepcopy(messages)
    record = complete(setup_transport, messages)
    assert record["error"] is None
    assert seen[0][0]["messages"] == before and messages == before
    assert seen[0][0]["tools"] == [{"type": "function", "function": TOOLS[0]}]
    assert seen[0][0]["tool_choice"] == "auto"
    assert seen[0][0]["thinking"] == {"type": "enabled"}
    assert seen[0][0]["reasoning_effort"] == "max" and seen[0][0]["temperature"] == 1
    assert record["assistant_message"] == assistant
    assert record["input_tokens"] == 130 and record["cached_input_tokens"] == 100
    assert record["total_tokens"] == 170 and record["reasoning_tokens"] == 30
    assert record["tools_used"] == 0
    assert record["native_tool_calls_requested"] == 1
    events = [json.loads(line) for line in (setup_transport.root / "calls.jsonl").read_text().splitlines()]
    assert [event["event"] for event in events] == ["started", "completed"]
    assert Path(record["raw_artifacts"]["response"]).exists()


def test_protocol_failures_remain_runner_responsibility(setup_transport, monkeypatch):
    reply, assistant = raw_reply(arguments='{"content":')
    monkeypatch.setattr(transport, "_http_exchange", lambda *a, **kw: reply)
    record = complete(setup_transport)
    assert record["error"] is None and record["assistant_message"] == assistant
    assert record["total_tokens"] == 170


def test_unknown_usage_and_plain_content_are_not_fabricated_or_finished(setup_transport, monkeypatch):
    reply = {"http_status": 200, "raw": json.dumps({"model": "glm-5.3", "choices": [
        {"finish_reason": "stop", "message": {"role": "assistant", "content": "DONE"}}]})}
    monkeypatch.setattr(transport, "_http_exchange", lambda *a, **kw: reply)
    record = complete(setup_transport)
    assert record["error"] is None and record["text"] == "DONE"
    assert record["total_tokens"] is None and record["cached_input_tokens"] is None
    assert "finished" not in record


def test_echo_mismatch_and_redaction_fail_closed_but_retain_usage(setup_transport, monkeypatch):
    reply, _ = raw_reply(model="wrong-model")
    monkeypatch.setattr(transport, "_http_exchange", lambda *a, **kw: reply)
    record = complete(setup_transport)
    assert record["error"]["code"] == "model_echo_mismatch" and record["total_tokens"] == 170
    reply, _ = raw_reply(content="api_key = example_fixture_token_123")
    record = complete(setup_transport)
    assert record["error"]["code"] == "action_redaction_mismatch"
    assert record["assistant_message"] is None and record["text"] == ""
    assert record["total_tokens"] == 170
    assert "example_fixture_token_123" not in (setup_transport.root / "calls.jsonl").read_text()


@pytest.mark.parametrize("code", ["socket_timeout", "total_deadline"])
def test_timeout_classification_and_redacted_error_logs(setup_transport, monkeypatch, code):
    def fail(*a, **kw):
        raise transport.TransportError(code, "fixture sk-fixture-credential-12345")
    monkeypatch.setattr(transport, "_http_exchange", fail)
    record = complete(setup_transport)
    assert record["timeout_kind"] == code and record["total_tokens"] is None
    assert "sk-fixture-credential-12345" not in json.dumps(record)


def test_gpt_delegates_unchanged_controlled_cli_and_does_not_add_final_prompt(setup_transport, monkeypatch):
    seen = []
    def cli(self, record, messages, folder):
        seen.append(copy.deepcopy(messages))
        record.update(text="DONE", input_tokens=100, output_tokens=3, total_tokens=103)
    monkeypatch.setattr(transport.original.ExperimentTransport, "_codex", cli)
    messages = [{"role": "system", "content": "runner's v2 tool-only completion protocol"}]
    record = complete(setup_transport, messages, model="gpt-5.6-sol")
    assert seen == [messages] and record["assistant_message"] == {"role": "assistant", "content": "DONE"}
    assert record["service_tier_requested"] == "fast" and record["effort_requested"] == "max"
    assert record["cap_enforced"] is False


def test_real_total_deadline_kills_owned_http_worker_even_with_continual_bytes(monkeypatch):
    received = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200); self.send_header("Content-Length", "1000"); self.end_headers()
            received.set()
            try:
                for _ in range(50):
                    self.wfile.write(b" "); self.wfile.flush(); time.sleep(.08)
            except OSError:
                pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    processes = []
    real_popen = transport.subprocess.Popen
    def capture(*a, **kw):
        process = real_popen(*a, **kw); processes.append(process); return process
    monkeypatch.setattr(transport.subprocess, "Popen", capture)
    started = time.monotonic()
    try:
        with pytest.raises(transport.TransportError) as exc:
            transport._http_exchange(f"http://127.0.0.1:{server.server_port}/", "{}", "fixture",
                                     socket_timeout_seconds=1, deadline_seconds=1.2)
        assert exc.value.code == "total_deadline"
        assert received.is_set() and processes[0].poll() is not None
        assert time.monotonic() - started < 3
    finally:
        server.shutdown(); server.server_close()
