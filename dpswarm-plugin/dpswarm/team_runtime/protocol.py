"""Provider-neutral tool response validation. This module never executes tools.

Only an entire text envelope is accepted; prose, fences, and legacy ``final``
messages are observations, never an instruction to finish a phase.
"""
from __future__ import annotations

import copy
import json
import math
import re
from typing import Any


class ProtocolError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> None:
    raise ProtocolError(code, message)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON property")
        result[key] = value
    return result


def _loads(text: str, code: str):
    try:
        return json.loads(text, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON number")))
    except (ValueError, TypeError, RecursionError) as exc:
        _fail(code, "Invalid JSON: " + str(exc))


_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}
_ANNOTATIONS = {"title", "description", "default", "examples", "$schema", "$id", "$comment", "deprecated", "readOnly", "writeOnly"}
_ASSERTIONS = {"type", "properties", "required", "additionalProperties", "enum", "const", "items", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength", "pattern", "minItems", "maxItems", "uniqueItems", "anyOf", "oneOf", "allOf"}


def _check_schema(schema: Any, path: str) -> None:
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict):
        _fail("invalid_tool_declaration", path + ": schema must be an object or boolean")
    unsupported = set(schema) - _ANNOTATIONS - _ASSERTIONS
    if unsupported:
        _fail("unsupported_schema", path + ": unsupported keywords " + ", ".join(sorted(unsupported)))
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not types or any(not isinstance(t, str) or t not in _TYPES for t in types):
            _fail("invalid_tool_declaration", path + ": invalid type")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict) or any(not isinstance(k, str) for k in properties):
        _fail("invalid_tool_declaration", path + ": invalid properties")
    for name, child in properties.items():
        _check_schema(child, path + "." + name)
    required = schema.get("required", [])
    if not isinstance(required, list) or any(not isinstance(k, str) for k in required) or len(set(required)) != len(required):
        _fail("invalid_tool_declaration", path + ": invalid required")
    for key in ("additionalProperties", "items"):
        if key in schema:
            _check_schema(schema[key], path + "." + key)
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        _fail("invalid_tool_declaration", path + ": invalid enum")
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            if not isinstance(schema[key], list) or not schema[key]:
                _fail("invalid_tool_declaration", path + ": invalid " + key)
            for child in schema[key]:
                _check_schema(child, path + "." + key)
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        if key in schema and (isinstance(schema[key], bool) or not isinstance(schema[key], (int, float)) or not math.isfinite(schema[key])):
            _fail("invalid_tool_declaration", path + ": invalid " + key)
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            _fail("invalid_tool_declaration", path + ": invalid " + key)
    if "uniqueItems" in schema and not isinstance(schema["uniqueItems"], bool):
        _fail("invalid_tool_declaration", path + ": invalid uniqueItems")
    if "pattern" in schema:
        try:
            re.compile(schema["pattern"])
        except (TypeError, re.error):
            _fail("invalid_tool_declaration", path + ": invalid pattern")


def normalize_tool_declarations(declarations) -> list[dict[str, Any]]:
    """Return Chat Completions function declarations without changing schemas."""
    if declarations is None:
        declarations = []
    if not isinstance(declarations, (list, tuple)):
        _fail("invalid_tool_declaration", "Tool declarations must be a list")
    result, names = [], set()
    for declaration in declarations:
        if not isinstance(declaration, dict):
            _fail("invalid_tool_declaration", "Tool declaration must be an object")
        if "function" in declaration:
            if declaration.get("type") != "function":
                _fail("invalid_tool_declaration", "Only function tools are supported")
            function = declaration["function"]
        else:
            function = {k: v for k, v in declaration.items() if k != "type"}
            if declaration.get("type", "function") != "function":
                _fail("invalid_tool_declaration", "Only function tools are supported")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str) or not function["name"].strip():
            _fail("invalid_tool_declaration", "Function name is missing")
        name = function["name"]
        if name in names:
            _fail("invalid_tool_declaration", "Duplicate function name: " + name)
        names.add(name)
        if "parameters" not in function:
            _fail("invalid_tool_declaration", "Function parameters are missing: " + name)
        _check_schema(function["parameters"], name)
        result.append({"type": "function", "function": copy.deepcopy(function)})
    return result


def _equal(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_equal(v, b[k]) for k, v in a.items())
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_equal(x, y) for x, y in zip(a, b))
    return a == b


def _is_type(value, kind):
    return {"object": isinstance(value, dict), "array": isinstance(value, list),
            "string": isinstance(value, str), "integer": type(value) is int or (type(value) is float and math.isfinite(value) and value.is_integer()),
            "number": type(value) in (int, float) and math.isfinite(value),
            "boolean": isinstance(value, bool), "null": value is None}[kind]


def _validate(value, schema, path):
    def invalid(detail):
        _fail("schema_validation_failed", path + ": " + detail)
    if schema is True:
        return
    if schema is False:
        invalid("value is forbidden")
    types = schema.get("type")
    if types is not None and not any(_is_type(value, t) for t in (types if isinstance(types, list) else [types])):
        invalid("type mismatch")
    if "enum" in schema and not any(_equal(value, option) for option in schema["enum"]):
        invalid("not in enum")
    if "const" in schema and not _equal(value, schema["const"]):
        invalid("does not match const")
    for keyword in ("anyOf", "oneOf", "allOf"):
        if keyword in schema:
            matches = 0
            for child in schema[keyword]:
                try:
                    _validate(value, child, path)
                    matches += 1
                except ProtocolError:
                    pass
            if not (matches >= 1 if keyword == "anyOf" else matches == 1 if keyword == "oneOf" else matches == len(schema[keyword])):
                invalid(keyword + " mismatch")
    if isinstance(value, dict):
        if any(key not in value for key in schema.get("required", [])):
            invalid("required property missing")
        properties = schema.get("properties", {})
        for key, child in value.items():
            if not isinstance(key, str):
                invalid("object property must be a string")
            _validate(child, properties.get(key, schema.get("additionalProperties", True)), path + "." + key)
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", math.inf):
            invalid("array length out of range")
        if schema.get("uniqueItems") and any(_equal(value[i], value[j]) for i in range(len(value)) for j in range(i)):
            invalid("duplicate array item")
        for i, child in enumerate(value):
            _validate(child, schema.get("items", True), path + f"[{i}]")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", math.inf):
            invalid("string length out of range")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            invalid("pattern mismatch")
    if type(value) in (int, float):
        if not math.isfinite(value):
            invalid("non-finite number")
        if value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            invalid("number out of range")
        if value <= schema.get("exclusiveMinimum", -math.inf) or value >= schema.get("exclusiveMaximum", math.inf):
            invalid("number outside exclusive range")


def _batch(calls, declarations, text):
    registry = {d["function"]["name"]: d["function"]["parameters"] for d in normalize_tool_declarations(declarations)}
    if not isinstance(calls, list) or not calls:
        _fail("invalid_calls", "Tool calls must be a nonempty list")
    result, seen = [], set()
    for call in calls:
        if not isinstance(call, dict):
            _fail("invalid_call", "Each tool call must be an object")
        cid, name, arguments = call.get("id"), call.get("name"), call.get("arguments")
        if not isinstance(cid, str) or not cid.strip():
            _fail("invalid_call_id", "Tool call ID must be a nonempty string")
        if cid in seen:
            _fail("duplicate_call_id", "Duplicate tool call ID")
        seen.add(cid)
        if not isinstance(name, str) or name not in registry:
            _fail("unknown_tool", "Tool name is not declared")
        if not isinstance(arguments, dict):
            _fail("invalid_arguments", "Tool arguments must be an object")
        # Validate JSON representability too, including nested NaN/Infinity.
        try:
            json.dumps(arguments, allow_nan=False)
        except (ValueError, TypeError, OverflowError):
            _fail("invalid_arguments", "Tool arguments must contain JSON values")
        _validate(arguments, registry[name], name)
        result.append({"id": cid, "name": name, "arguments": copy.deepcopy(arguments)})
    return {"kind": "tools", "calls": result, "text": text}


def parse_text_response(text: str, tool_declarations) -> dict[str, Any]:
    if not isinstance(text, str):
        _fail("invalid_response", "Text response must be a string")
    normalize_tool_declarations(tool_declarations)
    if not text.lstrip().startswith(("{", "[")):
        return {"kind": "no_action", "calls": [], "text": text}
    value = _loads(text, "invalid_json")
    if not isinstance(value, dict):
        _fail("invalid_response", "Text envelope must be one object")
    if value.get("type") in ("final", "no_action") and isinstance(value.get("content"), str):
        return {"kind": "no_action", "calls": [], "text": value["content"]}
    if value.get("type") != "tool_calls":
        _fail("invalid_response", "Unknown text envelope type")
    return _batch(value.get("calls"), tool_declarations, "")


def parse_native_response(message: dict[str, Any], tool_declarations) -> dict[str, Any]:
    if not isinstance(message, dict) or message.get("role", "assistant") != "assistant":
        _fail("invalid_response", "Native response must be an assistant message")
    text = message.get("content")
    if text is not None and not isinstance(text, str):
        _fail("invalid_response", "Assistant content must be text or null")
    normalize_tool_declarations(tool_declarations)
    calls = message.get("tool_calls")
    if calls is None or calls == []:
        return {"kind": "no_action", "calls": [], "text": text or ""}
    if not isinstance(calls, list):
        _fail("invalid_calls", "Tool calls must be a list")
    normalized = []
    for call in calls:
        if not isinstance(call, dict) or call.get("type") != "function" or not isinstance(call.get("function"), dict):
            _fail("invalid_call", "Only function tool calls are supported")
        function = call["function"]
        if not isinstance(function.get("arguments"), str):
            _fail("invalid_arguments_json", "Native function arguments must be a JSON string")
        normalized.append({"id": call.get("id"), "name": function.get("name"),
                           "arguments": _loads(function["arguments"], "invalid_arguments_json")})
    return _batch(normalized, tool_declarations, text or "")
