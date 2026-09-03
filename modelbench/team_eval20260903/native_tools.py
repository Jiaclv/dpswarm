"""GLM native-function adapter for the separate exploratory follow-up.

The frozen AgentLoop still owns every tool execution and its budget. Native
responses are normalized solely at that boundary; wire messages and reasoning
content are preserved for subsequent native tool turns.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import threading
from typing import Any
from urllib import error as urlerror, request

if __package__:
    from . import transports as original
else:
    import transports as original

NATIVE_MODELS = ("glm-5.3", "glm-5.3-flash")
ADAPTER_MODE = "glm_native_function_tools_v1"
GPT_MODE = "unchanged_codex_json_text_tools"
OFFICIAL_SOURCES = [
    "https://docs.bigmodel.cn/cn/guide/capabilities/function-calling",
    "https://docs.bigmodel.cn/cn/guide/capabilities/thinking-mode",
    "https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3",
    "https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash",
]
_SUFFIX = "\n\n" + original.TEXT_TOOL_PROTOCOL + "\nAvailable tools:\n"
_PROMPT_REPLACEMENTS = (
    ("All actual actions must be requested via the JSON tools; prose/code alone does not change files.",
     "All actual actions must be requested through the provided function tools; prose/code alone does not change files."),
    ("Finish using the JSON final response only after executing your role's work.",
     "Finish with a normal assistant response only after executing your role's work."),
)


class NativeActionRedactionMismatch(ValueError):
    """The frozen return path would mutate a proposed native action."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def extract_native_tools(system_text: str) -> tuple[str, list[dict[str, Any]], list[str]]:
    """Remove only the frozen transport instructions, retaining role/task policy."""
    if system_text.count(_SUFFIX) != 1:
        raise ValueError("Expected exactly one frozen TEXT_TOOL_PROTOCOL/schema suffix")
    system, schema_text = system_text.split(_SUFFIX, 1)
    schemas = json.loads(schema_text)
    if not isinstance(schemas, list) or not schemas:
        raise ValueError("Native tools require the frozen nonempty tool declarations")
    names = set()
    tools = []
    for schema in schemas:
        if not isinstance(schema, dict) or not isinstance(schema.get("name"), str):
            raise ValueError("Invalid frozen tool declaration")
        if schema["name"] in names or not isinstance(schema.get("parameters"), dict):
            raise ValueError("Duplicate tool name or missing parameters")
        names.add(schema["name"])
        tools.append({"type": "function", "function": deepcopy(schema)})
    changes = []
    for old, new in _PROMPT_REPLACEMENTS:
        if old in system:
            system = system.replace(old, new)
            changes.append(old)
    return system, tools, changes


def normalize_assistant(message: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Validate native wire shape and expose the frozen loop's JSON action shape."""
    if not isinstance(message, dict):
        raise ValueError("Native assistant message is not an object")
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise ValueError("Native assistant content must be text or null")
    wire: dict[str, Any] = {"role": "assistant", "content": content}
    if "reasoning_content" in message:
        wire["reasoning_content"] = deepcopy(message["reasoning_content"])
    native_calls = message.get("tool_calls")
    if native_calls:
        if not isinstance(native_calls, list) or not 1 <= len(native_calls) <= 8:
            raise ValueError("Native tool batch must contain between 1 and 8 calls")
        normalized = []
        wire_calls = []
        ids = set()
        for call in native_calls:
            if not isinstance(call, dict) or call.get("type") != "function":
                raise ValueError("Only native function tool calls are supported")
            call_id, function = call.get("id"), call.get("function")
            if not isinstance(call_id, str) or not call_id or call_id in ids:
                raise ValueError("Native tool call IDs must be nonempty and unique")
            if not isinstance(function, dict) or not isinstance(function.get("name"), str):
                raise ValueError("Native function name is missing")
            arguments_text = function.get("arguments")
            if not isinstance(arguments_text, str):
                raise ValueError("Native function.arguments must be a JSON string")
            arguments = json.loads(arguments_text)
            if not isinstance(arguments, dict):
                raise ValueError("Native function.arguments must decode to an object")
            ids.add(call_id)
            normalized.append({"id": call_id, "name": function["name"], "arguments": arguments})
            wire_calls.append(deepcopy(call))
        wire["tool_calls"] = wire_calls
        return _json({"type": "tool_calls", "calls": normalized}), wire
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Native response has neither function calls nor final text")
    return _json({"type": "final", "content": content}), wire


def convert_history(messages: list[dict[str, Any]], previous: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Pair each normalized assistant with its original native message and IDs.

    The caller passes only this run/role's previous responses. A mismatch fails
    before HTTP: reasoning or tool results are never fabricated to repair it.
    """
    wire = []
    tools = None
    assistant_index = 0
    pending: dict[str, str] = {}
    tool_result_count = 0
    reasoning_preserved = 0
    prompt_changes = []
    for message in messages:
        role, content = message.get("role"), message.get("content")
        if role == "system":
            if tools is not None or not isinstance(content, str):
                raise ValueError("Expected exactly one text system message")
            system, tools, prompt_changes = extract_native_tools(content)
            wire.append({"role": "system", "content": system})
        elif role == "assistant":
            if pending:
                raise ValueError("Previous native tool call has no paired tool result")
            if assistant_index >= len(previous):
                raise ValueError("Native assistant history is missing original wire evidence")
            prior = previous[assistant_index]
            if prior["normalized_text"] != content:
                raise ValueError("Normalized assistant history differs from original wire evidence")
            native = deepcopy(prior["wire_message"])
            wire.append(native)
            reasoning_preserved += int("reasoning_content" in native)
            pending = {call["id"]: call["function"]["name"] for call in native.get("tool_calls", [])}
            assistant_index += 1
        elif role == "user" and isinstance(content, str) and content.startswith("Tool results:\n"):
            results = json.loads(content[len("Tool results:\n"):])
            if not isinstance(results, list) or not pending:
                raise ValueError("Tool-result history has no pending native call")
            matched = set()
            for result in results:
                if not isinstance(result, dict):
                    raise ValueError("Tool result must be an object")
                call_id = result.get("id")
                if call_id not in pending or call_id in matched or result.get("name") != pending[call_id]:
                    raise ValueError("Native tool result ID/name does not match the requested call")
                matched.add(call_id)
                # Preserve the same already-truncated model-visible output and
                # metadata from the frozen loop. No extra output is retrieved.
                wire.append({"role": "tool", "tool_call_id": call_id, "content": _json(result)})
                tool_result_count += 1
            if matched != set(pending):
                raise ValueError("Native batch has missing tool results")
            pending = {}
        elif role == "user":
            if pending:
                raise ValueError("User message precedes unresolved native tool results")
            wire.append(deepcopy(message))
        else:
            raise ValueError("Unexpected role in frozen text history")
    if tools is None:
        raise ValueError("Frozen role system/tool declarations are missing")
    if assistant_index != len(previous) or pending:
        raise ValueError("Native history is incomplete or has unresolved tool calls")
    return wire, tools, {"assistant_messages_restored": assistant_index,
        "reasoning_messages_preserved": reasoning_preserved,
        "tool_results_paired": tool_result_count, "transport_instruction_replacements": prompt_changes}


class HybridTransport(original.ExperimentTransport):
    """GLM native functions; GPT delegates to the unchanged frozen CLI method."""

    def __init__(self, root: Path):
        super().__init__(root)
        self._native_previous: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._blocked_native_sessions: set[tuple[str, str]] = set()
        self._history_lock = threading.RLock()

    def _log(self, data: dict[str, Any], secrets: tuple[str, ...] = ()) -> None:
        data.setdefault("adapter_mode", ADAPTER_MODE if data.get("model_requested") in NATIVE_MODELS else GPT_MODE)
        super()._log(data, secrets)

    def _codex(self, record: dict[str, Any], messages: list[dict[str, Any]], folder: Path) -> None:
        record["adapter_mode"] = GPT_MODE
        super()._codex(record, messages, folder)

    def _glm(self, record: dict[str, Any], messages: list[dict[str, Any]], folder: Path, secrets: tuple[str, ...]) -> None:
        record["adapter_mode"] = ADAPTER_MODE
        record["native_tool_calls_requested"] = 0
        record["native_reasoning_preserved"] = True
        base = original._keyconfig("GLM_BASE_URL")
        if not base or "/coding/" not in base.rstrip("/") + "/":
            raise ValueError("GLM_BASE_URL must be the configured coding endpoint")
        endpoint = base.rstrip("/") + "/chat/completions"
        key = (record["run_id"], record["role"])
        with self._history_lock:
            if key in self._blocked_native_sessions:
                record["native_action_redaction_mismatch"] = True
                raise NativeActionRedactionMismatch(
                    "Native run/role session was blocked because frozen redaction would change a prior action")
            previous = deepcopy(self._native_previous.get(key, []))
        wire_messages, tools, audit = convert_history(messages, previous)
        record["native_history_audit"] = audit
        payload = {"model": record["model_requested"], "messages": wire_messages,
            "tools": tools, "tool_choice": "auto", "thinking": {"type": "enabled"},
            "reasoning_effort": "max", "temperature": 1.0,
            "max_tokens": record["max_tokens_requested"], "stream": False}
        request_path = folder / "wire.request.json"
        original._dump(request_path, {"endpoint": endpoint, "body": payload, "adapter_mode": ADAPTER_MODE}, secrets)
        record["raw_artifacts"]["request"] = str(request_path)
        record["raw_artifacts"]["wire_request"] = str(request_path)
        req = request.Request(endpoint, data=_json(payload).encode("utf-8"),
            headers={"Authorization": "Bearer " + secrets[0], "Content-Type": "application/json"}, method="POST")
        response_path = folder / "wire.response.raw.json"
        try:
            with request.urlopen(req, timeout=original.TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8", errors="replace")
                record["http_status"] = response.status
        except urlerror.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            record["http_status"] = exc.code
            response_path.write_text(original._redact(raw, secrets), encoding="utf-8")
            record["raw_artifacts"]["response"] = str(response_path)
            raise RuntimeError(f"GLM HTTP {exc.code}: {original._redact(raw, secrets)}") from None
        response_path.write_text(original._redact(raw, secrets), encoding="utf-8")
        record["raw_artifacts"]["response"] = str(response_path)
        record["raw_artifacts"]["wire_response"] = str(response_path)
        data = json.loads(raw)
        record["model_reported"] = data.get("model")
        record["service_tier_reported"] = data.get("service_tier")
        usage = data.get("usage") or {}
        record["input_tokens"] = original._integer(usage.get("prompt_tokens"))
        record["cached_input_tokens"] = original._integer((usage.get("prompt_tokens_details") or {}).get("cached_tokens"))
        record["output_tokens"] = original._integer(usage.get("completion_tokens"))
        record["reasoning_tokens"] = original._integer((usage.get("completion_tokens_details") or {}).get("reasoning_tokens"))
        record["total_tokens"] = original._integer(usage.get("total_tokens"))
        record["total_tokens_source"] = "glm.response.usage.total_tokens" if record["total_tokens"] is not None else None
        record["usage_source"] = "glm.response.usage" if usage else None
        choices = data.get("choices") or []
        if choices:
            record["stop_reason"] = choices[0].get("finish_reason")
        if record["model_reported"] != record["model_requested"]:
            raise RuntimeError(f"GLM model echo mismatch: requested {record['model_requested']!r}, reported {record['model_reported']!r}")
        if data.get("error") or not choices:
            raise RuntimeError("GLM returned error or no choices: " + _json(data.get("error")))
        message = choices[0].get("message")
        # Any native arguments/schema error is a consumed, logged failure. No
        # string repair, answer injection, model retry, or extra tools occur.
        normalized, wire_assistant = normalize_assistant(message)
        record["native_tool_calls_requested"] = len(wire_assistant.get("tool_calls", []))
        # original.complete redacts the serialized action before returning it to
        # AgentLoop. Never execute changed arguments while retaining different
        # native wire arguments in the next model request. Preserve usage/raw
        # response evidence above, then fail closed and disable this logical
        # session rather than silently continuing from divergent histories.
        if original._redact(normalized, secrets) != normalized:
            record["native_action_redaction_mismatch"] = True
            record["native_normalization_status"] = "blocked_redaction_would_change_action"
            with self._history_lock:
                self._blocked_native_sessions.add(key)
                self._native_previous.pop(key, None)
            raise NativeActionRedactionMismatch(
                "Frozen transport redaction would change the normalized native action; "
                "refusing execution and further history reuse for this run/role")
        record["text"] = normalized
        original._dump(folder / "wire.assistant.json", wire_assistant, secrets)
        original._dump(folder / "normalized.action.json", json.loads(normalized), secrets)
        record["raw_artifacts"]["wire_assistant"] = str(folder / "wire.assistant.json")
        record["raw_artifacts"]["normalized_action"] = str(folder / "normalized.action.json")
        with self._history_lock:
            self._native_previous.setdefault(key, []).append({
                "normalized_text": original._redact(normalized, secrets),
                "wire_message": wire_assistant, "call_id": record["call_id"]})


__all__ = ["HybridTransport", "NATIVE_MODELS", "ADAPTER_MODE", "GPT_MODE", "OFFICIAL_SOURCES",
           "extract_native_tools", "convert_history", "normalize_assistant"]
