import copy
import json

import pytest

from dpswarm.team_runtime.protocol import (
    ProtocolError, normalize_tool_declarations, parse_native_response,
    parse_text_response,
)


TOOLS = [{"name": "write", "parameters": {"type": "object", "required": ["path", "content"],
    "properties": {"path": {"type": "string", "minLength": 1}, "content": {"type": "string"}},
    "additionalProperties": False}},
    {"type": "function", "function": {"name": "finish_phase", "parameters": {"type": "object",
    "properties": {"summary": {"type": "string"}}, "required": ["summary"], "additionalProperties": False}}}]


def native(calls, text=None):
    return {"role": "assistant", "content": text, "reasoning_content": "fixture-reasoning",
            "tool_calls": [{"id": c.get("id"), "type": "function", "function": {
                "name": c.get("name"), "arguments": json.dumps(c.get("arguments"))}} for c in calls]}


def text(calls):
    return json.dumps({"type": "tool_calls", "calls": calls})


def test_native_and_text_share_validated_actions_and_do_not_mutate():
    calls = [{"id": "a", "name": "write", "arguments": {"path": "x.py", "content": 'print("quoted")\n'}},
             {"id": "b", "name": "finish_phase", "arguments": {"summary": "done"}}]
    wire = native(calls, "Here is the requested action.")
    before = copy.deepcopy(wire)
    assert parse_text_response(text(calls), TOOLS)["calls"] == calls
    parsed = parse_native_response(wire, TOOLS)
    assert parsed == {"kind": "tools", "calls": calls, "text": "Here is the requested action."}
    assert wire == before
    assert "finished" not in parsed  # finish_phase still needs runner execution.


@pytest.mark.parametrize("body", ["DONE", "I'll inspect first.", 'I will do this. {"type":"tool_calls","calls":[]}',
                                  '```json\n{"type":"tool_calls","calls":[]}\n```', ""])
def test_prose_and_fences_are_no_action_not_automatically_finished(body):
    assert parse_text_response(body, TOOLS) == parse_native_response({"content": body}, TOOLS)
    assert parse_text_response(body, TOOLS)["kind"] == "no_action"


def test_legacy_final_is_no_action():
    assert parse_text_response('{"type":"final","content":"DONE"}', TOOLS) == {
        "kind": "no_action", "calls": [], "text": "DONE"}


@pytest.mark.parametrize("calls,code", [
    ([{"id": "x", "name": "write", "arguments": {"path": "a", "content": ""}},
      {"id": "x", "name": "finish_phase", "arguments": {"summary": ""}}], "duplicate_call_id"),
    ([{"id": "", "name": "write", "arguments": {}}], "invalid_call_id"),
    ([{"id": "x", "name": "unknown", "arguments": {}}], "unknown_tool"),
    ([{"id": "x", "name": "write", "arguments": []}], "invalid_arguments"),
    ([{"id": "x", "name": "write", "arguments": {"path": "a"}}], "schema_validation_failed"),
    ([{"id": "x", "name": "write", "arguments": {"path": "a", "content": "", "extra": 1}}], "schema_validation_failed"),
    ([{"id": "x", "name": "write", "arguments": {"path": True, "content": ""}}], "schema_validation_failed"),
])
def test_whole_batch_has_equivalent_error_codes(calls, code):
    for parser, response in [(parse_text_response, text(calls)), (parse_native_response, native(calls))]:
        with pytest.raises(ProtocolError) as exc:
            parser(response, TOOLS)
        assert exc.value.code == code


def test_late_invalid_call_rejects_entire_batch():
    calls = [{"id": "good", "name": "write", "arguments": {"path": "x", "content": "safe"}},
             {"id": "bad", "name": "write", "arguments": {"path": "x"}}]
    with pytest.raises(ProtocolError, match="required property"):
        parse_native_response(native(calls), TOOLS)


@pytest.mark.parametrize("body", ['{"type":"tool_calls","calls":[]}}',
                                  '{"type":"tool_calls","type":"final","calls":[]}',
                                  '{"type":"tool_calls","calls":[]} trailing'])
def test_text_does_not_repair_malformed_or_trailing_json(body):
    with pytest.raises(ProtocolError) as exc:
        parse_text_response(body, TOOLS)
    assert exc.value.code == "invalid_json"


@pytest.mark.parametrize("arguments", ['{"x":1,"x":2}', '{"x":NaN}', '{"x":', '"unterminated'])
def test_native_argument_json_rejects_duplicates_nonfinite_and_malformed(arguments):
    wire = {"tool_calls": [{"id": "a", "type": "function", "function": {"name": "write", "arguments": arguments}}]}
    with pytest.raises(ProtocolError) as exc:
        parse_native_response(wire, TOOLS)
    assert exc.value.code == "invalid_arguments_json"


def test_nested_items_enum_boolean_and_additional_properties_schema():
    declarations = [{"name": "choose", "parameters": {"type": "object", "required": ["items"],
        "properties": {"items": {"type": "array", "minItems": 1, "items": {"type": "integer", "enum": [1, 2]}}},
        "additionalProperties": {"type": "boolean"}}}]
    good = [{"id": "a", "name": "choose", "arguments": {"items": [1, 2], "enabled": True}}]
    assert parse_native_response(native(good), declarations)["kind"] == "tools"
    for arguments in ({"items": [True]}, {"items": [3]}, {"items": [], "enabled": "yes"}):
        with pytest.raises(ProtocolError) as exc:
            parse_native_response(native([{**good[0], "arguments": arguments}]), declarations)
        assert exc.value.code == "schema_validation_failed"


def test_schema_declarations_are_normalized_and_unsupported_assertions_fail_closed():
    wrapped = normalize_tool_declarations(TOOLS)
    assert wrapped[0]["function"] == TOOLS[0]
    assert normalize_tool_declarations(wrapped) == wrapped
    with pytest.raises(ProtocolError) as exc:
        normalize_tool_declarations([{"name": "ref", "parameters": {"$ref": "#/defs/x"}}])
    assert exc.value.code == "unsupported_schema"
