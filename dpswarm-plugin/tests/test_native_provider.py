import copy
import json
from unittest.mock import Mock

import pytest

from dpswarm.providers.base import ProviderResult
from dpswarm.providers.openai_compat import OpenAICompatProvider
from dpswarm.types import ModelRoute, StopReason


TOOLS = [{"name": "read", "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                                         "required": ["path"], "additionalProperties": False}}]


def response(arguments='{"path":"x.py"}'):
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None, "reasoning_content": "fixture reasoning",
        "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "read", "arguments": arguments}}]}}],
        "usage": {"prompt_tokens": 110, "completion_tokens": 20, "total_tokens": 130,
                  "prompt_tokens_details": {"cached_tokens": 80},
                  "completion_tokens_details": {"reasoning_tokens": 12}}}


def test_legacy_provider_result_constructor_still_works():
    result = ProviderResult("ok", StopReason.COMPLETED)
    assert result.assistant_message is None and result.usage.total_tokens() == 0
    assert result.usage_observation == {}


def test_native_tools_are_a_completed_transport_with_explicit_continuation():
    raw = response()
    result = OpenAICompatProvider._parse_response(raw, TOOLS)
    assert result.stop_reason == StopReason.COMPLETED
    assert result.response_kind == "tools" and result.continuation
    assert result.finish_reason == "tool_calls"
    assert result.assistant_message == raw["choices"][0]["message"]
    assert result.usage.input_tokens == 30 and result.usage.cache_read_tokens == 80
    assert result.usage.total_tokens() == 130
    assert result.usage_observation["input_tokens"] == 110
    assert result.usage_observation["reasoning_tokens"] == 12
    assert result.usage_observation["cost_usd"] is None
    raw["choices"][0]["message"]["reasoning_content"] = "modified"
    assert result.assistant_message["reasoning_content"] == "fixture reasoning"


@pytest.mark.parametrize("arguments,code", [('{"path":', "invalid_arguments_json"),
                                           ('{"path":4}', "schema_validation_failed")])
def test_malformed_native_keeps_usage_and_reasoning_evidence(arguments, code):
    result = OpenAICompatProvider._parse_response(response(arguments), TOOLS)
    assert result.stop_reason == StopReason.ERROR and not result.continuation
    assert result.protocol_error["code"] == code
    assert result.usage_observation["total_tokens"] == 130
    assert result.assistant_message["reasoning_content"] == "fixture reasoning"


def test_missing_usage_remains_unknown_despite_legacy_integer_placeholders():
    raw = response(); del raw["usage"]
    result = OpenAICompatProvider._parse_response(raw, TOOLS)
    assert result.usage.total_tokens() == 0
    assert result.usage_observation["total_tokens"] is None
    assert result.usage_observation["input_tokens"] is None
    assert result.usage_observation["complete"] is False


def test_plain_content_and_length_do_not_signal_tool_continuation():
    raw = {"choices": [{"finish_reason": "stop", "message": {"content": "DONE"}}]}
    result = OpenAICompatProvider._parse_response(raw, TOOLS)
    assert result.response_kind == "no_action" and not result.continuation
    raw = response(); raw["choices"][0]["finish_reason"] = "length"
    result = OpenAICompatProvider._parse_response(raw, TOOLS)
    assert result.stop_reason == StopReason.MAX_TOKENS and not result.continuation


def test_wire_body_preserves_native_history_and_wraps_standard_tools():
    provider = OpenAICompatProvider(base_url="https://fixture.invalid/v1", api_key="fixture")
    route = ModelRoute(provider="fixture", model="fixture-model")
    messages = [{"role": "assistant", **response()["choices"][0]["message"]},
                {"role": "tool", "tool_call_id": "call_1", "content": "file contents"}]
    before = copy.deepcopy(messages)
    reply = Mock(); reply.__enter__ = Mock(return_value=reply); reply.__exit__ = Mock(return_value=False)
    reply.read.return_value = json.dumps(response()).encode()
    provider._opener.open = Mock(return_value=reply)
    result = provider.complete(route, messages, tools=TOOLS)
    sent = json.loads(provider._opener.open.call_args.args[0].data)
    assert sent["messages"] == before and messages == before
    assert sent["tools"][0] == {"type": "function", "function": TOOLS[0]}
    assert sent["tool_choice"] == "auto" and result.continuation
