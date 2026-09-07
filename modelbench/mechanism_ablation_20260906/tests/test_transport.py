"""Fake process/HTTP results with real request shaping and response admission."""
import json
from pathlib import Path
import subprocess

import pytest

from modelbench.mechanism_ablation_20260906 import transport as t
from modelbench.big_budget_team_20260906.transport import CodingPlanTransport

TOOLS = [{"type": "function", "function": {"name": "finish", "description": "fixture",
          "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}}}]


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("GLM_API_KEY", "fixture-glm-key")
    monkeypatch.setenv("GLM_BASE_URL", t._runtime.CODING_GLM_BASE_URL)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-deepseek-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    return t.AblationTransport(tmp_path / "calls")


def call(adapter, model="gpt-5.6-sol", **changes):
    kwargs = dict(tools=TOOLS, run_id="fixture-run", role="lead", task_id="fixture-task",
                  call_id="fixture-call", max_tokens=32768, timeout_seconds=600)
    kwargs.update(changes)
    return adapter.complete(model, [{"role": "user", "content": "Offline fixture"}], **kwargs)


@pytest.mark.parametrize("violation", [False, True])
def test_codex_exposure_is_constrained_and_violation_rejects_all_actions(adapter, monkeypatch, violation):
    text = json.dumps({"type": "tool_calls", "calls": [{"id": "done", "name": "finish", "arguments": {"summary": "READY"}}]})
    events = [{"type": "item.completed", "item": {"id": "answer", "type": "agent_message", "text": text}},
              {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20}}]
    if violation:
        events.insert(0, {"type": "item.completed", "item": {"id": "forbidden", "type": "mcp_tool_call",
                         "server": "fixture", "tool": "list_mcp_resources"}})
    observed = []
    def run(argv, **kwargs):
        observed.append(argv)
        kwargs["record"]["transport_attempt_count"] = 1
        Path(argv[argv.index("--output-last-message") + 1]).write_text(text, encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "\n".join(json.dumps(e) for e in events) + "\n", "")
    monkeypatch.setattr(t._runtime, "_run_process_original", run)
    result = call(adapter)
    assert isinstance(adapter, CodingPlanTransport)
    assert len(observed) == 1
    argv = observed[0]
    for feature in t.DISABLED_NATIVE_FEATURES:
        assert any(argv[i:i+2] == ["--disable", feature] for i in range(len(argv)-1))
    assert "mcp_servers={}" in argv and 'approval_policy="never"' in argv
    assert result["total_tokens"] == 120 and result["usage_complete"]
    request = json.loads(Path(result["raw_artifacts"]["request"]).read_text(encoding="utf-8"))
    assert request["argv"] == argv
    assert result["native_tool_policy"]["native_execution_allowed"] is False
    if violation:
        assert result["error"] and result["parsed_calls"] == [] and result["assistant_message"] is None
    else:
        assert result["error"] is None and result["parsed_calls"][0]["name"] == "finish"


def test_deepseek_cm_wire_is_unchanged_and_not_coding_channel(adapter, monkeypatch):
    seen = []
    def run(argv, **kwargs):
        kwargs["record"]["transport_attempt_count"] = 1
        request = json.loads(kwargs["input"])
        seen.append(request)
        body = {"model": "deepseek-v4-flash", "choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": "Fixture summary"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}
        return subprocess.CompletedProcess(argv, 0, json.dumps({"http_status": 200, "raw": json.dumps(body)}), "")
    monkeypatch.setattr(t._runtime, "_run_process_original", run)
    result = call(adapter, "deepseek-v4-flash", tools=[], role="cm", max_tokens=4096)
    request = seen[0]
    body = json.loads(request["payload"])
    assert request["endpoint"] == "https://api.deepseek.com/chat/completions"
    assert request["socket_timeout_seconds"] == 120
    assert body["thinking"] == {"type": "disabled"} and body["stream"] is False
    assert body["max_tokens"] == 4096 and body["tools"] == []
    assert result["error"] is None and result["action"]["text"] == "Fixture summary"
    assert result["usage_complete"] and result["total_tokens"] == 120


def test_private_transport_does_not_replace_historical_hooks():
    from modelbench.big_budget_team_20260906 import transport as original
    assert t._runtime is not original and t._runtime._base is not original._base
    assert original._base._load_v2 is original._load_v2
