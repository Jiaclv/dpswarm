"""Offline checks of transport evidence and failure boundaries."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from modelbench.team_eval20260903.transports import ExperimentTransport


class TransportTests(unittest.TestCase):
    def test_codex_usage_includes_cache_and_actual_fields_remain_unknown(self):
        events = [
            {"type": "thread.started", "thread_id": "example"},
            {"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "OK"}},
            {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 7}},
        ]
        completed = subprocess.CompletedProcess([], 0, "\n".join(map(json.dumps, events)), "")
        with tempfile.TemporaryDirectory() as directory, patch("modelbench.team_eval20260903.transports._run_codex_process", return_value=completed) as run:
            result = ExperimentTransport(Path(directory)).complete("gpt-5.6-luna", [], run_id="r", role="x", task_id="t")
            self.assertIsNone(result["error"])
            self.assertEqual((result["input_tokens"], result["cached_input_tokens"], result["total_tokens"]), (100, 60, 107))
            self.assertIsNone(result["model_reported"])
            self.assertIsNone(result["service_tier_reported"])
            self.assertFalse(result["cap_enforced"])
            self.assertIn("--ignore-user-config", run.call_args.args[0])
            self.assertEqual(run.call_args.kwargs["timeout"], 600)
            log = [json.loads(line) for line in (Path(directory) / "calls.jsonl").read_text().splitlines()]
            self.assertEqual([item["event"] for item in log], ["started", "completed"])

    def test_tool_event_fails_even_with_answer(self):
        events = [
            {"type": "item.started", "item": {"id": "tool-1", "type": "command_execution"}},
            {"type": "item.completed", "item": {"id": "tool-1", "type": "command_execution"}},
            {"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "OK"}},
            {"type": "turn.completed", "usage": {}},
        ]
        completed = subprocess.CompletedProcess([], 0, "\n".join(map(json.dumps, events)), "")
        with tempfile.TemporaryDirectory() as directory, patch("modelbench.team_eval20260903.transports._run_codex_process", return_value=completed):
            result = ExperimentTransport(Path(directory)).complete("gpt-5.6-sol", [], run_id="r", role="x", task_id="t")
            self.assertEqual(result["tools_used"], 1)
            self.assertIn("violation", result["error"]["message"])

    def test_timeout_preserves_partial_events_and_redacts_error(self):
        error = subprocess.TimeoutExpired([], 600, output=b'{"type":"error","message":"Reconnecting 1/5"}\n', stderr=b'Bearer sk-example_secret_value')
        with tempfile.TemporaryDirectory() as directory, patch("modelbench.team_eval20260903.transports._run_codex_process", side_effect=error):
            result = ExperimentTransport(Path(directory)).complete("gpt-5.6-terra", [], run_id="r", role="x", task_id="t")
            self.assertEqual(result["error"]["type"], "TimeoutError")
            self.assertTrue(result["reconnect_detected"])
            self.assertNotIn("sk-example_secret_value", Path(result["raw_artifacts"]["stderr"]).read_text())

    def test_glm_echo_mismatch_is_logged_failure(self):
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self):
                return json.dumps({"model": "wrong", "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                                   "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}).encode()
        keyconfig = lambda name: "test-private-secret" if name == "GLM_API_KEY" else "https://example.test/api/coding/paas/v4"
        with tempfile.TemporaryDirectory() as directory, patch("modelbench.team_eval20260903.transports._keyconfig", side_effect=keyconfig), patch("modelbench.team_eval20260903.transports.request.urlopen", return_value=Response()) as urlopen:
            result = ExperimentTransport(Path(directory)).complete("glm-5.3", [], run_id="r", role="x", task_id="t")
            self.assertIn("echo mismatch", result["error"]["message"])
            self.assertEqual(result["input_tokens"], 10)
            self.assertIsNone(result["cached_input_tokens"])
            payload = json.loads(urlopen.call_args.args[0].data)
            self.assertEqual(payload["reasoning_effort"], "max")
            self.assertEqual(payload["thinking"], {"type": "enabled"})
            self.assertNotIn("test-private-secret", Path(result["raw_artifacts"]["request"]).read_text())

    def test_unknown_model_is_captured(self):
        with tempfile.TemporaryDirectory() as directory:
            result = ExperimentTransport(Path(directory)).complete("unknown", [], run_id="r", role="x", task_id="t")
            self.assertEqual(result["error"]["type"], "ValueError")
            self.assertTrue(Path(result["raw_artifacts"]["metadata"]).exists())

    def test_answer_about_retries_is_not_a_reconnect_event(self):
        events = [
            {"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "Please retry the failing unit test."}},
            {"type": "turn.completed", "usage": {}},
        ]
        completed = subprocess.CompletedProcess([], 0, "\n".join(map(json.dumps, events)), "")
        with tempfile.TemporaryDirectory() as directory, patch("modelbench.team_eval20260903.transports._run_codex_process", return_value=completed):
            result = ExperimentTransport(Path(directory)).complete("gpt-5.6-luna", [], run_id="r", role="x", task_id="t")
            self.assertFalse(result["reconnect_detected"])
            self.assertFalse(result["retry_attempted"])


if __name__ == "__main__":
    unittest.main()
