"""Offline history, usage, transport-parity and manifest tests; no model calls."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import native_tools as native
import native_followup as followup

SCHEMAS = [
    {"name": "read", "description": "Read a file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "run", "description": "Run a command", "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}},
]


def system():
    body = ("Keep Executor permissions and task facts unchanged. "
        "All actual actions must be requested via the JSON tools; prose/code alone does not change files. "
        "Batch independent actions where useful; up to 8 tools per model response. "
        "Finish using the JSON final response only after executing your role's work.")
    return body + "\n\n" + native.original.TEXT_TOOL_PROTOCOL + "\nAvailable tools:\n" + json.dumps(SCHEMAS)


def first_messages():
    return [{"role": "system", "content": system()}, {"role": "user", "content": "Fresh task brief"}]


def assistant(call_id="id-1", name="read", args=None):
    return {"role": "assistant", "content": "Inspecting the fresh workspace.",
            "reasoning_content": "Synthetic private reasoning fixture.",
            "tool_calls": [{"id": call_id, "type": "function", "function": {
                "name": name, "arguments": json.dumps(args or {"path": "/shared/workspace/code.py"})}}]}


def response(message=None, model="glm-5.3", usage=None):
    return {"model": model, "choices": [{"finish_reason": "tool_calls" if message and message.get("tool_calls") else "stop",
        "message": message or {"role": "assistant", "content": "Done", "reasoning_content": "Synthetic final fixture"}}],
        "usage": usage or {"prompt_tokens": 1234, "prompt_tokens_details": {"cached_tokens": 800},
            "completion_tokens": 57, "completion_tokens_details": {"reasoning_tokens": 50}, "total_tokens": 1291}}


class HTTPResponse:
    status = 200
    def __init__(self, data): self.data = data
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def read(self): return json.dumps(self.data).encode("utf-8")


def config(name):
    return "synthetic-private-key" if name == "GLM_API_KEY" else "https://example.test/api/coding/paas/v4"


class NativeTests(unittest.TestCase):
    def test_exact_schema_and_transport_only_system_conversion(self):
        original = system()
        converted, tools, replacements = native.extract_native_tools(original)
        self.assertEqual([item["function"] for item in tools], SCHEMAS)
        self.assertNotIn(native.original.TEXT_TOOL_PROTOCOL, converted)
        self.assertIn("Keep Executor permissions and task facts unchanged", converted)
        self.assertIn("up to 8 tools per model response", converted)
        self.assertEqual(len(replacements), 2)
        self.assertIn("normal assistant response", converted)

    def test_native_ids_arguments_and_reasoning_history_pairing(self):
        raw = assistant(args={"path": "quote\"and\\slash.py"})
        normalized, wire = native.normalize_assistant(raw)
        previous = [{"normalized_text": normalized, "wire_message": wire}]
        result = {"name": "read", "id": "id-1", "stdout": "truncated output", "stderr": "", "exit_code": 0}
        messages = first_messages() + [{"role": "assistant", "content": normalized},
            {"role": "user", "content": "Tool results:\n" + json.dumps([result])},
            {"role": "user", "content": "Phase executor: model call 2/6."}]
        before = deepcopy(messages)
        converted, tools, audit = native.convert_history(messages, previous)
        self.assertEqual(messages, before)
        self.assertEqual(converted[2], raw)
        self.assertEqual(converted[3]["role"], "tool")
        self.assertEqual(converted[3]["tool_call_id"], "id-1")
        self.assertEqual(json.loads(converted[3]["content"]), result)
        self.assertEqual(audit["reasoning_messages_preserved"], 1)
        self.assertEqual(audit["tool_results_paired"], 1)
        self.assertEqual(json.loads(normalized)["calls"][0]["arguments"], {"path": "quote\"and\\slash.py"})

    def test_missing_or_wrong_result_id_fails_closed(self):
        normalized, wire = native.normalize_assistant(assistant())
        previous = [{"normalized_text": normalized, "wire_message": wire}]
        history = first_messages() + [{"role": "assistant", "content": normalized}]
        with self.assertRaises(ValueError):
            native.convert_history(history, previous)
        with self.assertRaises(ValueError):
            native.convert_history(history + [{"role": "user", "content": "Tool results:\n" + json.dumps([
                {"id": "wrong-id", "name": "read", "stdout": "x", "stderr": "", "exit_code": 0}])}], previous)
        with self.assertRaises(ValueError):
            native.convert_history(history, [])

    def test_invalid_arguments_and_oversized_batch_are_not_repaired(self):
        raw = assistant()
        raw["tool_calls"][0]["function"]["arguments"] = '{"path":"bad\\q"}'
        with self.assertRaises(ValueError): native.normalize_assistant(raw)
        raw = assistant()
        raw["tool_calls"] = [assistant(call_id=str(index))["tool_calls"][0] for index in range(9)]
        with self.assertRaises(ValueError): native.normalize_assistant(raw)
        raw["tool_calls"] = [assistant()["tool_calls"][0], assistant()["tool_calls"][0]]
        with self.assertRaises(ValueError): native.normalize_assistant(raw)

    def test_two_rounds_preserve_wire_and_usage_without_executing_tools(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(native.original, "_keyconfig", side_effect=config), patch.object(native.request, "urlopen", side_effect=[HTTPResponse(response(assistant())), HTTPResponse(response())]) as http:
            transport = native.HybridTransport(Path(directory))
            one = transport.complete("glm-5.3", first_messages(), run_id="fresh-1", role="executor", task_id="fixture")
            self.assertIsNone(one["error"])
            history = first_messages() + [{"role": "assistant", "content": one["text"]},
                {"role": "user", "content": "Tool results:\n" + json.dumps([
                    {"id": "id-1", "name": "read", "stdout": "fixture file", "stderr": "", "exit_code": 0}])},
                {"role": "user", "content": "Continue"}]
            two = transport.complete("glm-5.3", history, run_id="fresh-1", role="executor", task_id="fixture")
            self.assertIsNone(two["error"])
            self.assertEqual(json.loads(two["text"]), {"type": "final", "content": "Done"})
            self.assertEqual((one["input_tokens"], one["cached_input_tokens"], one["output_tokens"], one["reasoning_tokens"], one["total_tokens"]), (1234, 800, 57, 50, 1291))
            self.assertEqual(one["tools_used"], 0)
            self.assertEqual(one["native_tool_calls_requested"], 1)
            self.assertEqual(two["adapter_mode"], native.ADAPTER_MODE)
            request = json.loads(http.call_args_list[1].args[0].data)
            self.assertEqual(request["messages"][2], assistant())
            self.assertEqual(request["messages"][3]["tool_call_id"], "id-1")
            self.assertEqual(request["tool_choice"], "auto")
            self.assertEqual(request["reasoning_effort"], "max")
            self.assertEqual(request["thinking"], {"type": "enabled"})
            self.assertEqual(request["temperature"], 1.0)
            self.assertEqual(request["max_tokens"], 32768)
            self.assertEqual(http.call_count, 2)
            self.assertNotIn("synthetic-private-key", Path(one["raw_artifacts"]["wire_request"]).read_text())
            self.assertTrue(Path(one["raw_artifacts"]["wire_response"]).is_file())
            records = [json.loads(line) for line in (Path(directory) / "calls.jsonl").read_text().splitlines()]
            self.assertEqual([r["event"] for r in records], ["started", "completed", "started", "completed"])
            self.assertTrue(all(r["adapter_mode"] == native.ADAPTER_MODE for r in records))

    def test_history_isolation_final_then_repair_and_unknown_usage(self):
        replies = [HTTPResponse(response()), HTTPResponse(response()), HTTPResponse(response(usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}))]
        with tempfile.TemporaryDirectory() as directory, patch.object(native.original, "_keyconfig", side_effect=config), patch.object(native.request, "urlopen", side_effect=replies) as http:
            transport = native.HybridTransport(Path(directory))
            first = transport.complete("glm-5.3", first_messages(), run_id="one", role="executor", task_id="fixture")
            other = transport.complete("glm-5.3", first_messages(), run_id="two", role="executor", task_id="fixture")
            self.assertIsNone(other["error"])
            repair = transport.complete("glm-5.3", first_messages() + [{"role": "assistant", "content": first["text"]},
                {"role": "user", "content": "New verifier feedback from this run"}], run_id="one", role="executor", task_id="fixture")
            self.assertIsNone(repair["error"])
            self.assertIsNone(repair["cached_input_tokens"])
            self.assertIsNone(repair["reasoning_tokens"])
            payload = json.loads(http.call_args_list[2].args[0].data)
            self.assertEqual(payload["messages"][2]["content"], "Done")
            self.assertIn("reasoning_content", payload["messages"][2])

    def test_echo_mismatch_is_recorded_once_with_usage(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(native.original, "_keyconfig", side_effect=config), patch.object(native.request, "urlopen", return_value=HTTPResponse(response(model="wrong"))) as http:
            result = native.HybridTransport(Path(directory)).complete("glm-5.3", first_messages(), run_id="one", role="executor", task_id="fixture")
            self.assertIn("echo mismatch", result["error"]["message"])
            self.assertEqual(result["total_tokens"], 1291)
            self.assertEqual(http.call_count, 1)

    def test_action_redaction_never_silently_changes_code_or_native_history(self):
        normal_code = "print('ordinary fixture source')\n"
        keylike_code = "api_key = example_fixture_token_123\n"
        replies = [HTTPResponse(response(assistant(name="write", args={"path": "code.py", "content": normal_code}))),
                   HTTPResponse(response(assistant(name="write", args={"path": "code.py", "content": keylike_code})))]
        with tempfile.TemporaryDirectory() as directory, patch.object(native.original, "_keyconfig", side_effect=config), patch.object(native.request, "urlopen", side_effect=replies) as http:
            transport = native.HybridTransport(Path(directory))
            normal = transport.complete("glm-5.3", first_messages(), run_id="ordinary", role="executor", task_id="fixture")
            self.assertIsNone(normal["error"])
            executed_arguments = json.loads(normal["text"])["calls"][0]["arguments"]
            cached_arguments = json.loads(transport._native_previous[("ordinary", "executor")][0]["wire_message"]["tool_calls"][0]["function"]["arguments"])
            self.assertEqual(executed_arguments, cached_arguments)
            self.assertEqual(executed_arguments["content"], normal_code)
            blocked = transport.complete("glm-5.3", first_messages(), run_id="keylike", role="executor", task_id="fixture")
            self.assertEqual(blocked["error"]["type"], "NativeActionRedactionMismatch")
            self.assertEqual(blocked["text"], "")
            self.assertTrue(blocked["native_action_redaction_mismatch"])
            self.assertEqual(blocked["total_tokens"], 1291)
            self.assertTrue(Path(blocked["raw_artifacts"]["wire_response"]).is_file())
            self.assertNotIn(("keylike", "executor"), transport._native_previous)
            continued = transport.complete("glm-5.3", first_messages(), run_id="keylike", role="executor", task_id="fixture")
            self.assertEqual(continued["error"]["type"], "NativeActionRedactionMismatch")
            self.assertIsNone(continued["input_tokens"])
            self.assertEqual(http.call_count, 2)

    def test_gpt_frozen_cli_settings_and_text_protocol_remain_unchanged(self):
        events = [{"type": "item.completed", "item": {"type": "agent_message", "text": '{"type":"final","content":"Done"}'}},
                  {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 3}}]
        completed = subprocess.CompletedProcess([], 0, "\n".join(map(json.dumps, events)), "")
        with tempfile.TemporaryDirectory() as directory, patch.object(native.original, "_run_codex_process", return_value=completed) as run:
            result = native.HybridTransport(Path(directory)).complete("gpt-5.6-sol", first_messages(), run_id="one", role="planner", task_id="fixture")
            self.assertIsNone(result["error"])
            self.assertEqual(result["adapter_mode"], native.GPT_MODE)
            self.assertIn('model_reasoning_effort="max"', run.call_args.args[0])
            self.assertIn('service_tier="fast"', run.call_args.args[0])
            submitted = json.loads(run.call_args.kwargs["input"].split("\n\n", 1)[1])
            self.assertEqual(submitted["messages"], first_messages())
            self.assertIn(native.original.TEXT_TOOL_PROTOCOL, submitted["messages"][0]["content"])
            self.assertEqual(result["input_tokens"], 10)
            self.assertFalse(result["cap_enforced"])

    def test_manifest_copies_only_frozen_instances_and_freezes_four_runs(self):
        before = followup.read_main_manifest()
        main_hash = followup.sha(HERE / "manifest.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "followup"
            manifest = followup.prepare(root)
            self.assertEqual(manifest["experiment_kind"], "native_tools_followup")
            self.assertEqual(manifest["experiment_label"], "GLM 原生工具调用探索性复测")
            self.assertFalse(manifest["preregistered_main_score"])
            self.assertEqual(len(manifest["schedule"]), 4)
            self.assertEqual(manifest["limits"], before["limits"])
            self.assertEqual(manifest["run_token_boundary"], before["run_token_boundary"])
            self.assertEqual(manifest["image_id"], before["image_id"])
            self.assertEqual(manifest["main_manifest_sha256"], main_hash)
            self.assertEqual(manifest["instances"], manifest["main_instances"])
            self.assertFalse((root / "results").exists())
            self.assertFalse((root / "calls.jsonl").exists())
            self.assertEqual(followup.prepare(root), manifest)
            (root / "instances" / followup.TASKS[0] / "task" / "brief.md").write_text("tampered")
            with self.assertRaises(ValueError): followup.prepare(root)
        self.assertEqual(followup.sha(HERE / "manifest.json"), main_hash)

    def test_run_gate_waits_for_every_main_result(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(followup, "read_main_manifest", return_value={"schedule": [{"run_id": "a"}, {"run_id": "b"}]}):
            root = Path(directory)
            (root / "results" / "a").mkdir(parents=True)
            (root / "results" / "a" / "result.json").write_text("{}")
            with self.assertRaises(RuntimeError): followup.assert_main_completed(root)
            (root / "results" / "b").mkdir(parents=True)
            (root / "results" / "b" / "result.json").write_text("{}")
            followup.assert_main_completed(root)

    def test_original_teamrun_factory_and_actual_role_prompt_in_separate_process(self):
        code = """import json, sys
from pathlib import Path
import native_followup as followup
import native_tools as native
root = Path(sys.argv[1])
runner = followup.configured_runner(root)
run = runner.TeamRun(followup.schedule(followup.read_main_manifest())[0])
system, declarations = run.config('executor')
full = system + '\\n\\n' + native.original.TEXT_TOOL_PROTOCOL + '\\nAvailable tools:\\n' + json.dumps(declarations)
converted, tools, changes = native.extract_native_tools(full)
assert runner.ROOT == root.resolve()
assert runner.ExperimentTransport is native.HybridTransport
assert run.folder.is_relative_to(root)
assert [item['function'] for item in tools] == declarations
assert len(changes) == 2
assert run.model_map['planner'] == 'gpt-5.6-sol'
assert run.model_map['verifier'] == 'gpt-5.6-terra'
print('TEAMRUN_NATIVE_COMPATIBLE')
"""
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run([sys.executable, "-c", code, directory],
                cwd=HERE, capture_output=True, text=True, check=True)
            self.assertEqual(completed.stdout.strip(), "TEAMRUN_NATIVE_COMPATIBLE")


if __name__ == "__main__":
    unittest.main()
