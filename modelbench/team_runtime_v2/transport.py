"""Audited v2 transport. The runner owns native history and protocol decisions.

GLM HTTP runs in an owned child process with a real total deadline; killing the
child cancels the local request. A remote provider may still finish/bill work
already accepted. No retries, model substitution, or tool execution occurs.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib import error as urlerror, request
from uuid import uuid4

_MAIN = Path(__file__).resolve().parents[1] / "team_eval20260903" / "transports.py"
_spec = importlib.util.spec_from_file_location("_frozen_team_v2_transport_base", _MAIN)
original = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(original)
_PLUGIN = Path(__file__).resolve().parents[2] / "dpswarm-plugin"
if str(_PLUGIN) not in sys.path:
    sys.path.insert(0, str(_PLUGIN))
from dpswarm.team_runtime.protocol import normalize_tool_declarations

MODELS = original.MODELS
TOTAL_DEADLINE_SECONDS = 600.0
SOCKET_TIMEOUT_SECONDS = 300.0


class TransportError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _http_worker():
    """Private subprocess entry; credentials arrive via stdin, never argv/logs."""
    job = json.loads(sys.stdin.read())
    req = request.Request(job["endpoint"], data=job["payload"].encode("utf-8"),
                          headers={"Content-Type": "application/json",
                                   "Authorization": "Bearer " + job["key"]}, method="POST")
    try:
        with request.urlopen(req, timeout=job["socket_timeout_seconds"]) as response:
            result = {"http_status": response.status,
                      "raw": response.read().decode("utf-8", errors="replace")}
    except urlerror.HTTPError as exc:
        result = {"http_status": exc.code, "raw": exc.read().decode("utf-8", errors="replace"),
                  "error_code": "http_error", "error_message": "HTTP " + str(exc.code)}
    except (OSError, ValueError) as exc:
        reason = exc.reason if isinstance(exc, urlerror.URLError) else exc
        code = "socket_timeout" if isinstance(reason, (TimeoutError, socket.timeout)) else "network_error"
        result = {"error_code": code, "error_message": type(exc).__name__ + ": " + str(exc)}
    sys.stdout.write(json.dumps(result, ensure_ascii=True))


def _http_exchange(endpoint, payload, key, *, socket_timeout_seconds, deadline_seconds):
    """One cancellable attempt. No background thread can outlive this call."""
    if deadline_seconds <= 0:
        raise TransportError("total_deadline", "Total call deadline elapsed before HTTP start")
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    process = subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()), "--http-worker"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace", **options)
    job = json.dumps({"endpoint": endpoint, "payload": payload, "key": key,
                      "socket_timeout_seconds": socket_timeout_seconds}, ensure_ascii=True)
    try:
        stdout, stderr = process.communicate(job, timeout=deadline_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=5)
        raise TransportError("total_deadline", "Total call deadline exceeded; owned HTTP process terminated") from None
    if process.returncode:
        # Worker stderr may contain reflected request data; parent always redacts.
        raise TransportError("http_worker_error", "HTTP worker failed: " + stderr[-1000:])
    try:
        result = json.loads(stdout)
    except ValueError:
        raise TransportError("http_worker_error", "HTTP worker returned invalid JSON") from None
    if not isinstance(result, dict):
        raise TransportError("http_worker_error", "HTTP worker returned a non-object")
    return result


class V2Transport(original.ExperimentTransport):
    """complete(..., tools=...) returns nullable measurements and wire assistant.

    Text/argument schemas are intentionally parsed by the runner. A successful
    transport response is not a phase completion or accepted work artifact.
    """

    def complete(self, model, messages, *, tools, run_id, role, task_id, max_tokens=32768):
        call_id = str(uuid4())
        folder = self.root / "calls" / call_id
        folder.mkdir(parents=True, exist_ok=False)
        started, start_clock = original._now(), time.monotonic()
        glm = isinstance(model, str) and model.startswith("glm-")
        record = {
            "call_id": call_id, "run_id": run_id, "role": role, "task_id": task_id,
            "text": "", "assistant_message": None, "stop_reason": None,
            "model_requested": model, "model_reported": None, "effort_requested": "max",
            "effort_reported": None, "service_tier_requested": None if glm else "fast",
            "service_tier_reported": None, "input_tokens": None, "cached_input_tokens": None,
            "output_tokens": None, "reasoning_tokens": None, "total_tokens": None,
            "usage_source": None, "total_tokens_source": None, "input_tokens_includes_cached": True,
            "wall_seconds": None, "error": None, "raw_artifacts": {}, "tools_used": 0,
            "started_at": started, "completed_at": None,
            "timestamps": {"started_at": started, "completed_at": None},
            "max_tokens_requested": max_tokens, "cap_enforced": glm,
            "retry_attempted": False, "reconnect_detected": False, "reconnect_events": [],
            "attempt_count": 1, "transport_attempt_count": 1,
            "adapter_mode": "glm_native_tools_v2" if glm else "codex_text_tools_v2",
            "total_deadline_seconds": TOTAL_DEADLINE_SECONDS if glm else original.TIMEOUT_SECONDS,
            "socket_timeout_seconds": SOCKET_TIMEOUT_SECONDS if glm else None,
            "timeout_kind": None,
        }
        self._log({"event": "started", **record})
        secrets = ()
        try:
            if model not in MODELS:
                raise TransportError("unsupported_model", "Unsupported model")
            if type(max_tokens) is not int or max_tokens <= 0:
                raise TransportError("invalid_request", "max_tokens must be a positive integer")
            if not isinstance(messages, list) or any(not isinstance(m, dict) for m in messages):
                raise TransportError("invalid_request", "messages must be a list of message objects")
            declarations = normalize_tool_declarations(tools)
            if glm:
                key = original._keyconfig("GLM_API_KEY")
                if not key:
                    raise TransportError("missing_credentials", "GLM_API_KEY is missing from keyconfig")
                secrets = (key,)
            original._dump(folder / "prompt.json", {"model": model, "messages": messages,
                           "tools": declarations, "effort": "max", "max_tokens": max_tokens,
                           "service_tier": record["service_tier_requested"]}, secrets)
            record["raw_artifacts"]["prompt"] = str(folder / "prompt.json")
            if glm:
                self._glm_v2(record, messages, declarations, folder, secrets,
                             TOTAL_DEADLINE_SECONDS - (time.monotonic() - start_clock))
            else:
                # The runner supplies the v2 text protocol. This adds only the
                # frozen CLI safety/transport envelope; no v1 final instruction.
                original.ExperimentTransport._codex(self, record, messages, folder)
                if not record.get("error"):
                    record["assistant_message"] = {"role": "assistant", "content": record["text"]}
            assistant = record.get("assistant_message")
            if assistant is not None and original._redact(assistant, secrets) != assistant:
                record["assistant_message"] = None
                record["text"] = ""
                raise TransportError("action_redaction_mismatch", "Logging redaction would change assistant history or action; response cannot be executed")
        except Exception as exc:
            record["error"] = {"type": type(exc).__name__, "code": getattr(exc, "code", "transport_error"),
                               "message": str(exc)}
            if getattr(exc, "code", None) in ("socket_timeout", "total_deadline"):
                record["timeout_kind"] = exc.code
            record["assistant_message"] = None
            record["text"] = ""
        finally:
            record["wall_seconds"] = round(time.monotonic() - start_clock, 6)
            record["completed_at"] = original._now()
            record["timestamps"]["completed_at"] = record["completed_at"]
            (folder / "output.md").write_text(original._redact(record["text"], secrets), encoding="utf-8")
            record["raw_artifacts"]["output"] = str(folder / "output.md")
            record["raw_artifacts"]["metadata"] = str(folder / "metadata.json")
            if record.get("assistant_message") is not None:
                original._dump(folder / "assistant.json", record["assistant_message"], secrets)
                record["raw_artifacts"]["assistant_message"] = str(folder / "assistant.json")
            record = original._redact(record, secrets)
            original._dump(folder / "metadata.json", record, secrets)
            self._log({"event": "completed", **record}, secrets)
        return record

    def _glm_v2(self, record, messages, declarations, folder, secrets, deadline_seconds):
        deadline_at = time.monotonic() + deadline_seconds
        base = original._keyconfig("GLM_BASE_URL")
        if not base or "/coding/" not in base.rstrip("/") + "/":
            raise TransportError("invalid_endpoint", "GLM_BASE_URL must be the configured coding endpoint")
        endpoint = base.rstrip("/") + "/chat/completions"
        payload = {"model": record["model_requested"], "messages": copy.deepcopy(messages),
                   "tools": declarations, "tool_choice": "auto",
                   "thinking": record.get("glm_thinking") or {"type": "enabled"},
                   "reasoning_effort": "max", "temperature": 1.0,
                   "max_tokens": record["max_tokens_requested"], "stream": False}
        serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        original._dump(folder / "wire.request.json", {"endpoint": endpoint, "body": payload}, secrets)
        record["request_body_sha256"] = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        record["raw_artifacts"]["request"] = str(folder / "wire.request.json")
        response = _http_exchange(endpoint, serialized, secrets[0],
                                  socket_timeout_seconds=SOCKET_TIMEOUT_SECONDS,
                                  deadline_seconds=deadline_at - time.monotonic())
        record["http_status"] = response.get("http_status")
        if "raw" in response:
            raw = response["raw"]
            (folder / "wire.response.raw.json").write_text(original._redact(raw, secrets), encoding="utf-8")
            record["raw_artifacts"]["response"] = str(folder / "wire.response.raw.json")
        if response.get("error_code"):
            raise TransportError(response["error_code"], response.get("error_message", "HTTP request failed"))
        try:
            data = json.loads(response["raw"])
        except (KeyError, ValueError, TypeError):
            raise TransportError("invalid_wire_json", "GLM response is not a JSON object") from None
        if not isinstance(data, dict):
            raise TransportError("invalid_wire_json", "GLM response is not a JSON object")
        record["model_reported"] = data.get("model")
        record["service_tier_reported"] = data.get("service_tier")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        details = usage.get("prompt_tokens_details") or {}
        output_details = usage.get("completion_tokens_details") or {}
        record["input_tokens"] = original._integer(usage.get("prompt_tokens"))
        record["cached_input_tokens"] = original._integer(details.get("cached_tokens")) if isinstance(details, dict) else None
        record["output_tokens"] = original._integer(usage.get("completion_tokens"))
        record["reasoning_tokens"] = original._integer(output_details.get("reasoning_tokens")) if isinstance(output_details, dict) else None
        record["total_tokens"] = original._integer(usage.get("total_tokens"))
        record["usage_source"] = "glm.response.usage" if usage else None
        record["total_tokens_source"] = "glm.response.usage.total_tokens" if record["total_tokens"] is not None else None
        if record["total_tokens"] is None and record["input_tokens"] is not None and record["output_tokens"] is not None:
            record["total_tokens"] = record["input_tokens"] + record["output_tokens"]
            record["total_tokens_source"] = "derived_input_plus_output"
        if record["model_reported"] != record["model_requested"]:
            raise TransportError("model_echo_mismatch", "GLM model echo does not match the requested model")
        choices = data.get("choices")
        if data.get("error") or not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise TransportError("invalid_wire_response", "GLM returned an error or no valid choice")
        record["stop_reason"] = choices[0].get("finish_reason")
        assistant = choices[0].get("message")
        if not isinstance(assistant, dict) or assistant.get("role", "assistant") != "assistant":
            raise TransportError("invalid_wire_response", "GLM choice is not an assistant message")
        if assistant.get("content") is not None and not isinstance(assistant["content"], str):
            raise TransportError("invalid_wire_response", "GLM assistant content must be text or null")
        # No argument parsing or schema validation here: the runner applies the
        # same ProtocolError policy to native and text responses.
        record["assistant_message"] = copy.deepcopy(assistant)
        record["assistant_message"].setdefault("role", "assistant")
        record["text"] = assistant.get("content") or ""
        calls = assistant.get("tool_calls")
        record["native_tool_calls_requested"] = len(calls) if isinstance(calls, list) else None


if __name__ == "__main__":
    if sys.argv[1:] != ["--http-worker"]:
        raise SystemExit("This module is a transport library")
    _http_worker()
