"""Auditable, stdlib-only model transport for the 2026-09-03 experiment.

The caller owns the agent loop. This module never executes a model's proposed
tools. Codex's own tools are disabled and any observed tool event fails the call.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib import error as urlerror, request
from uuid import uuid4

MODELS = ("glm-5.3", "glm-5.3-flash", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
CODEX_JS = Path(r"C:\Users\93711\AppData\Roaming\npm\node_modules\@openai\codex\bin\codex.js")
TIMEOUT_SECONDS = 600
TEXT_TOOL_PROTOCOL = '''Return exactly one JSON object, without Markdown fences.
For a final response: {"type":"final","content":"your response"}.
For requested external tools: {"type":"tool_calls","calls":[{"id":"unique call id","name":"tool name","arguments":{}}]}.
Only request the tools described in the messages. The external AgentTeam loop
executes authorized tools in the role's container and returns their results.
Do not execute tools yourself. These instructions do not override any higher
priority instructions or built-in safety requirements.'''

_LOG_LOCK = threading.Lock()
_SECRET_PATTERN = re.compile(
    r"(?i)(?:(?:sk|sk-proj|sk-ant|sess)-[a-z0-9_\-]{8,}|"
    r"eyJ[a-z0-9_\-]{12,}\.[a-z0-9_\-]+\.[a-z0-9_\-]+|"
    r"(?:bearer\s+)[a-z0-9_.\-]{8,}|"
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization)"
    r"[\s\"':=]+[^\s\",;}]{8,})"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return _SECRET_PATTERN.sub("[REDACTED]", value)
    if isinstance(value, list):
        return [_redact(v, secrets) for v in value]
    if isinstance(value, dict):
        return {str(k): _redact(v, secrets) for k, v in value.items()}
    return value


def _dump(path: Path, value: Any, secrets: tuple[str, ...] = ()) -> None:
    path.write_text(json.dumps(_redact(value, secrets), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _keyconfig(name: str) -> str | None:
    path = Path(__file__).resolve().parents[1] / "keyconfig.py"
    spec = importlib.util.spec_from_file_location("_team_eval_keyconfig", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load modelbench/keyconfig.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.get(name)


def _tool_item(item: dict[str, Any]) -> bool:
    kind = str(item.get("type", ""))
    return (kind in {"command_execution", "file_change", "web_search", "image_view", "shell_command"}
            or any(word in kind for word in ("tool_call", "function_call", "computer_call")))


def _run_codex_process(argv: list[str], *, input: str, cwd: Path,
                       timeout: int = TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    """Bound the entire launcher + native CLI process tree, preserving output."""
    options: dict[str, Any] = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding="utf-8",
                               errors="replace", cwd=cwd, **options)
    try:
        stdout, stderr = process.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # codex.js spawns codex.exe. Killing only node on Windows leaves the
        # native model request alive, so terminate precisely this owned tree.
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=15, check=False,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            os.killpg(process.pid, signal.SIGKILL)
        try:
            stdout, stderr = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = exc.stdout or "", exc.stderr or ""
        raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr) from None
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


class ExperimentTransport:
    """One independent request per complete(); failures are returned and logged.

    ``root`` is an artifact directory, not the repository or a model working
    directory. ``input_tokens`` includes cached input tokens for both providers.
    Null fields indicate that the provider did not expose the measurement.
    """

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _log(self, data: dict[str, Any], secrets: tuple[str, ...] = ()) -> None:
        with _LOG_LOCK:
            with (self.root / "calls.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(_redact(data, secrets), ensure_ascii=False) + "\n")

    def complete(self, model: str, messages: list[dict[str, Any]], *, run_id: str,
                 role: str, task_id: str, max_tokens: int = 32768) -> dict[str, Any]:
        call_id = str(uuid4())
        folder = self.root / "calls" / call_id
        folder.mkdir(parents=True, exist_ok=False)
        started = _now()
        clock_start = time.monotonic()
        record: dict[str, Any] = {
            "call_id": call_id, "run_id": run_id, "role": role, "task_id": task_id,
            "text": "", "stop_reason": None, "model_requested": model,
            "model_reported": None, "effort_requested": "max", "effort_reported": None,
            "service_tier_requested": "fast" if model.startswith("gpt-") else None,
            "service_tier_reported": None, "input_tokens": None,
            "cached_input_tokens": None, "output_tokens": None, "reasoning_tokens": None,
            "total_tokens": None, "usage_source": None, "wall_seconds": None,
            "total_tokens_source": None, "input_tokens_includes_cached": True,
            "error": None, "raw_artifacts": {}, "tools_used": 0,
            "started_at": started, "completed_at": None,
            "timestamps": {"started_at": started, "completed_at": None},
            "max_tokens_requested": max_tokens, "cap_enforced": not model.startswith("gpt-"),
            "retry_attempted": False, "reconnect_detected": False,
            "reconnect_events": [], "attempt_count": 1,
            "transport_attempt_count": 1,
        }
        secrets: tuple[str, ...] = ()
        self._log({"event": "started", **record})
        try:
            if model not in MODELS:
                raise ValueError("Unsupported model; expected one of " + ", ".join(MODELS))
            if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
                raise ValueError("max_tokens must be a positive integer")
            if not isinstance(messages, list) or any(not isinstance(m, dict) for m in messages):
                raise ValueError("messages must be a list of message objects")
            if model.startswith("glm-"):
                key = _keyconfig("GLM_API_KEY")
                if not key:
                    raise RuntimeError("GLM_API_KEY is missing from keyconfig")
                secrets = (key,)
            _dump(folder / "prompt.json", {"model": model, "messages": messages,
                  "effort": "max", "max_tokens": max_tokens,
                  "service_tier": record["service_tier_requested"]}, secrets)
            record["raw_artifacts"]["prompt"] = str(folder / "prompt.json")
            if model.startswith("glm-"):
                self._glm(record, messages, folder, secrets)
            else:
                self._codex(record, messages, folder)
        except Exception as exc:
            record["error"] = _redact({"type": type(exc).__name__, "message": str(exc)}, secrets)
        finally:
            record["wall_seconds"] = round(time.monotonic() - clock_start, 6)
            record["completed_at"] = _now()
            record["timestamps"]["completed_at"] = record["completed_at"]
            (folder / "output.md").write_text(_redact(record["text"], secrets), encoding="utf-8")
            record["raw_artifacts"]["output"] = str(folder / "output.md")
            record["raw_artifacts"]["metadata"] = str(folder / "metadata.json")
            record = _redact(record, secrets)
            _dump(folder / "metadata.json", record, secrets)
            self._log({"event": "completed", **record}, secrets)
        return record

    def _glm(self, record: dict[str, Any], messages: list[dict[str, Any]],
             folder: Path, secrets: tuple[str, ...]) -> None:
        base = _keyconfig("GLM_BASE_URL")
        if not base:
            raise RuntimeError("GLM_BASE_URL is missing from keyconfig")
        if "/coding/" not in base.rstrip("/") + "/":
            raise ValueError("GLM_BASE_URL must be the configured coding endpoint")
        endpoint = base.rstrip("/") + "/chat/completions"
        payload = {"model": record["model_requested"], "messages": messages,
                   "thinking": {"type": "enabled"}, "reasoning_effort": "max",
                   "temperature": 1.0, "max_tokens": record["max_tokens_requested"],
                   "stream": False}
        _dump(folder / "request.json", {"endpoint": endpoint, "body": payload}, secrets)
        record["raw_artifacts"]["request"] = str(folder / "request.json")
        req = request.Request(endpoint, data=json.dumps(payload).encode("utf-8"),
                              headers={"Authorization": "Bearer " + secrets[0],
                                       "Content-Type": "application/json"}, method="POST")
        try:
            with request.urlopen(req, timeout=TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8", errors="replace")
                record["http_status"] = response.status
        except urlerror.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            record["http_status"] = exc.code
            (folder / "response.raw.json").write_text(_redact(raw, secrets), encoding="utf-8")
            record["raw_artifacts"]["response"] = str(folder / "response.raw.json")
            raise RuntimeError(f"GLM HTTP {exc.code}: {_redact(raw, secrets)}") from None
        (folder / "response.raw.json").write_text(_redact(raw, secrets), encoding="utf-8")
        record["raw_artifacts"]["response"] = str(folder / "response.raw.json")
        data = json.loads(raw)
        record["model_reported"] = data.get("model")
        record["service_tier_reported"] = data.get("service_tier")
        usage = data.get("usage") or {}
        record["input_tokens"] = _integer(usage.get("prompt_tokens"))
        record["cached_input_tokens"] = _integer((usage.get("prompt_tokens_details") or {}).get("cached_tokens"))
        record["output_tokens"] = _integer(usage.get("completion_tokens"))
        record["reasoning_tokens"] = _integer((usage.get("completion_tokens_details") or {}).get("reasoning_tokens"))
        record["total_tokens"] = _integer(usage.get("total_tokens"))
        record["total_tokens_source"] = "glm.response.usage.total_tokens" if record["total_tokens"] is not None else None
        record["usage_source"] = "glm.response.usage" if usage else None
        choices = data.get("choices") or []
        if choices:
            choice = choices[0]
            message = choice.get("message") or {}
            record["text"] = message.get("content") or ""
            record["stop_reason"] = choice.get("finish_reason")
            if message.get("tool_calls"):
                raise RuntimeError("GLM emitted native tool calls instead of the required text-only protocol")
        if record["model_reported"] != record["model_requested"]:
            raise RuntimeError(f"GLM model echo mismatch: requested {record['model_requested']!r}, reported {record['model_reported']!r}")
        if data.get("error"):
            raise RuntimeError("GLM response error: " + json.dumps(data["error"], ensure_ascii=False))
        if not choices:
            raise RuntimeError("GLM returned no choices")

    def _codex(self, record: dict[str, Any], messages: list[dict[str, Any]], folder: Path) -> None:
        cwd = folder / "empty-cwd"
        cwd.mkdir()
        argv = ["node", str(CODEX_JS), "exec", "--ignore-user-config", "--json",
                "--skip-git-repo-check", "--ephemeral", "-s", "read-only",
                "-m", record["model_requested"],
                "-c", 'model_reasoning_effort="max"', "-c", 'service_tier="fast"',
                "-c", 'approval_policy="never"', "--disable", "shell_tool",
                "--disable", "multi_agent", "-c", 'web_search="disabled"', "-"]
        prompt = ("Respond to the conversation serialized below. Do not execute tools. "
                  "If the conversation asks for tools, express requests only as the specified JSON text "
                  "for an external loop to execute in the role container. "
                  "These instructions do not override built-in safety or higher-priority instructions.\n\n"
                  + json.dumps({"messages": messages}, ensure_ascii=False))
        _dump(folder / "request.json", {"argv": argv, "cwd": str(cwd), "stdin": prompt,
                                       "cap_enforced": False, "timeout_seconds": TIMEOUT_SECONDS})
        record["raw_artifacts"]["request"] = str(folder / "request.json")
        timed_out = False
        try:
            result = _run_codex_process(argv, input=prompt, cwd=cwd, timeout=TIMEOUT_SECONDS)
            stdout, stderr, returncode = result.stdout, result.stderr, result.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            returncode = None
        (folder / "stdout.jsonl").write_text(_redact(stdout), encoding="utf-8")
        (folder / "stderr.txt").write_text(_redact(stderr), encoding="utf-8")
        record["raw_artifacts"].update(stdout=str(folder / "stdout.jsonl"), stderr=str(folder / "stderr.txt"))
        record["returncode"] = returncode
        events = []
        parse_errors = []
        for line in stdout.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("event is not an object")
                events.append(event)
            except ValueError:
                parse_errors.append(line)
        tool_ids = set()
        texts = []
        turn_completed = False
        failures = []
        for index, event in enumerate(events):
            kind = event.get("type", "")
            item = event.get("item") or {}
            if isinstance(item, dict) and _tool_item(item):
                tool_ids.add(str(item.get("id", f"event-{index}")))
            if _tool_item(event):
                tool_ids.add(str(event.get("id", f"event-{index}")))
            if kind == "item.completed" and item.get("type") == "agent_message":
                texts.append(item.get("text", ""))
            if kind == "turn.completed":
                turn_completed = True
                usage = event.get("usage") or {}
                record["input_tokens"] = _integer(usage.get("input_tokens"))
                record["cached_input_tokens"] = _integer(usage.get("cached_input_tokens"))
                record["output_tokens"] = _integer(usage.get("output_tokens"))
                record["reasoning_tokens"] = _integer(usage.get("reasoning_tokens"))
                record["total_tokens"] = _integer(usage.get("total_tokens"))
                record["total_tokens_source"] = "codex.turn.completed.usage.total_tokens" if record["total_tokens"] is not None else None
                if record["total_tokens"] is None and record["input_tokens"] is not None and record["output_tokens"] is not None:
                    record["total_tokens"] = record["input_tokens"] + record["output_tokens"]
                    record["total_tokens_derived"] = True
                    record["total_tokens_source"] = "derived: input_tokens + output_tokens"
                record["usage_source"] = "codex.turn.completed.usage" if usage else None
                record["stop_reason"] = event.get("stop_reason") or "turn.completed"
            if kind in {"error", "turn.failed"}:
                failures.append(event)
            for key in ("model", "model_slug"):
                if isinstance(event.get(key), str):
                    record["model_reported"] = event[key]
            if isinstance(event.get("service_tier"), str):
                record["service_tier_reported"] = event["service_tier"]
        record["text"] = "\n".join(texts)
        record["tools_used"] = len(tool_ids)
        record["event_count"] = len(events)
        # Model-generated prose can legitimately discuss retries. Only provider
        # error/status events and stderr are evidence of transport reconnects.
        reconnect_candidates = stderr.splitlines() + [
            json.dumps(event, ensure_ascii=False) for event in events
            if event.get("type") in {"error", "turn.failed", "warning", "status", "reconnecting", "retry"}
        ]
        reconnect_lines = [line for line in reconnect_candidates
                           if re.search(r"(?i)reconnect(?:ing|ed)?|retry(?:ing| attempt)?|attempt\s+[2-9]\b", line)]
        record["reconnect_detected"] = bool(reconnect_lines)
        record["retry_attempted"] = bool(reconnect_lines)
        record["reconnect_events"] = _redact(reconnect_lines)
        if reconnect_lines:
            # The JSON interface does not expose enough evidence to count every
            # internal HTTP/WebSocket attempt. The adapter itself never retries.
            record["attempt_count"] = None
        if failures:
            record["provider_error_events"] = _redact(failures)
        if parse_errors:
            record["non_json_stdout_lines"] = _redact(parse_errors)
        if timed_out:
            raise TimeoutError(f"Codex exceeded {TIMEOUT_SECONDS} seconds")
        if tool_ids:
            raise RuntimeError("Codex tool-execution violation: " + ", ".join(sorted(tool_ids)))
        if returncode != 0:
            raise RuntimeError(f"Codex exited {returncode}: {_redact(stderr[-4000:])}")
        if failures:
            raise RuntimeError("Codex emitted error/failed events: " + json.dumps(_redact(failures), ensure_ascii=False))
        if not turn_completed:
            raise RuntimeError("Codex output has no turn.completed event")
        if not texts:
            raise RuntimeError("Codex output has no completed agent_message")
        if record["model_reported"] is not None and record["model_reported"] != record["model_requested"]:
            raise RuntimeError("Codex reported a different model than requested")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run five tiny, unscored transport preflight calls")
    parser.add_argument("--preflight", action="store_true", required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent / "preflight")
    args = parser.parse_args()
    transport = ExperimentTransport(args.root)
    results = []
    for model in MODELS:
        result = transport.complete(model, [{"role": "user", "content": "Reply exactly OK"}],
                                    run_id="unscored-preflight", role="preflight", task_id="exactly-OK",
                                    max_tokens=1024)
        results.append(result)
        print(json.dumps({"model": model, "text": result["text"], "error": result["error"],
                          "wall_seconds": result["wall_seconds"], "call_id": result["call_id"]}), flush=True)
    _dump(args.root / "summary.json", {"scored": False, "results": results})
