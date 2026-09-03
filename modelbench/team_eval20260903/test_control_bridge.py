"""Simulated grading fixtures exercise the real CP journal and invariants."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from modelbench.team_eval20260903.control_bridge import ExperimentControl
from dpswarm.control import ControlPlane, ControlPlaneError
from dpswarm import invariants, state


MODELS = {"planner": "gpt-5.6-sol", "executor": "glm-5.3", "verifier": "gpt-5.6-terra"}
TASK_ID = "SPEC5_config_system"


def call(role, model, call_id, **changes):
    result = {"call_id": call_id, "role": role, "task_id": TASK_ID,
              "model_requested": model, "input_tokens": 100, "cached_input_tokens": 40,
              "output_tokens": 12, "reasoning_tokens": None, "total_tokens": 112,
              "stop_reason": "stop", "wall_seconds": 0.1, "error": None}
    result.update(changes)
    return result


def grading(run_dir, passed=True):
    path = run_dir / "oracle-grade" / "score.raw.json"
    path.parent.mkdir()
    raw = {"pass": passed, "secondary": {"partial_score": 1.0 if passed else 0.0}}
    path.write_text(json.dumps(raw), encoding="utf-8")
    vendor = Path(__file__).parent / "TeamBench"
    content = subprocess.run(["git", "-C", str(vendor), "show",
        "d185aef1916fd86a9ba554d581fd256319a973af:tasks/" + TASK_ID + "/grade.sh"],
        check=True, capture_output=True).stdout
    script = run_dir / "task" / "grade.sh"
    script.parent.mkdir()
    script.write_bytes(content)
    return {"raw_score": raw, "score": raw, "raw_score_path": str(path),
            "grade_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
            "exit_code": 0, "timed_out": False, "score_parse_error": None}


class ControlBridgeTests(unittest.TestCase):
    def test_top_level_import_matches_driver_entrypoint(self):
        process = subprocess.run([sys.executable, "-c",
            "from control_bridge import ExperimentControl; print(ExperimentControl.__name__)"],
            cwd=Path(__file__).parent, capture_output=True, text=True, check=True)
        self.assertEqual(process.stdout.strip(), "ExperimentControl")

    def test_all_roles_counted_submit_waits_for_oracle_and_disk_replays(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = ExperimentControl(root, MODELS, TASK_ID)
            try:
                for index, (role, model) in enumerate(MODELS.items()):
                    handle = bridge.start_role(role, "initial")
                    entry = call(role, model, str(index))
                    bridge.record_call(handle, entry)
                    bridge.record_call(handle, entry)  # idempotent call_id
                    bridge.submit_role(handle, {"text": "fixture submission"})
                    self.assertEqual(bridge.cp.proj.work_items[handle.item_id].acceptance.value, "submitted")
                self.assertFalse(any(event.kind == "work_item_accepted" for event in bridge.cp.store.read_all()))
                usage = bridge.get_usage()
                self.assertEqual(usage["total"]["input_tokens"], 300)
                self.assertEqual(usage["total"]["total_tokens"], 336)
                self.assertEqual(usage["total"]["calls"], 3)
                self.assertEqual(set(usage["by_role"]), set(MODELS))
                token_events = [e for e in bridge.cp.store.read_all() if e.kind == "token_usage_recorded"]
                self.assertEqual(len(token_events), 3)
                self.assertEqual(token_events[0].payload["input"], 60)
                self.assertEqual(token_events[0].payload["cache_read"], 40)
                self.assertIsNone(token_events[0].payload["cost"])
                result = bridge.finish(grading(root), {"fixture": True})
                self.assertTrue(result["control_accepted"])
                self.assertTrue(result["invariant_replay_passed"])
                self.assertEqual(result["cp"]["active_points"], 0)
                self.assertEqual(result["cp"]["open_worker_slots_used"], 0)
                self.assertEqual(result["cp"]["seal_phase"]["root"], "completed")
                expected = bridge.cp.snapshot()
            finally:
                bridge.close()
            replay = ControlPlane(store_path=root / "control-plane" / "events.jsonl", catalog=bridge.catalog)
            try:
                self.assertEqual(replay.snapshot(), expected)
                checked = state.Projection()
                for event in replay.store.read_all():
                    checked = invariants.check_event(checked, event)
                self.assertEqual(checked.active_points, 0)
            finally:
                replay.close()

    def test_failed_oracle_never_accepts_root_or_roles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = ExperimentControl(root, {"executor": "glm-5.3"}, TASK_ID)
            try:
                handle = bridge.start_role("executor", "initial")
                bridge.submit_role(handle, {"text": "fixture wrong answer"})
                result = bridge.finish(grading(root, False), {"fixture": True})
                self.assertFalse(result["task_pass"])
                self.assertFalse(result["control_accepted"])
                self.assertFalse(any(e.kind == "work_item_accepted" for e in bridge.cp.store.read_all()))
                self.assertTrue(all(i.acceptance.value == "terminated" for i in bridge.cp.proj.work_items.values()))
                self.assertEqual(result["cp"]["active_points"], 0)
            finally:
                bridge.close()

    def test_unknown_cache_and_usage_stay_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = ExperimentControl(Path(directory), {"executor": "glm-5.3"}, TASK_ID)
            try:
                handle = bridge.start_role("executor", "initial")
                bridge.record_call(handle, call("executor", "glm-5.3", "unknown-cache", cached_input_tokens=None))
                bridge.record_call(handle, call("executor", "glm-5.3", "failed", input_tokens=None,
                    cached_input_tokens=None, output_tokens=None, total_tokens=None, error={"type": "TimeoutError"}))
                usage = bridge.get_usage()["total"]
                self.assertIsNone(usage["input_tokens"])
                self.assertEqual(usage["known_sums"]["input_tokens"], 100)
                self.assertEqual(usage["unknown_counts"]["input_tokens"], 1)
                self.assertEqual(usage["failed_calls"], 1)
                token = next(e for e in bridge.cp.store.read_all() if e.kind == "token_usage_recorded")
                self.assertIsNone(token.payload["input"])
                self.assertIsNone(token.payload["cache_read"])
                self.assertIsNone(token.payload["cost"])
            finally:
                bridge.close()

    def test_stale_session_fence_prevents_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = ExperimentControl(Path(directory), {"executor": "glm-5.3"}, TASK_ID)
            try:
                handle = bridge.start_role("executor", "initial")
                bridge.cp.begin_rollover(handle.node_id, handle.manifest_path, handle.manifest_hash)
                bridge.cp.confirm_rollover(handle.node_id, session_id="new-session")
                before = bridge.cp.store.last_seq
                with self.assertRaises(ControlPlaneError) as error:
                    bridge.submit_role(handle, {"text": "stale"})
                self.assertEqual(error.exception.code, "FENCE_VIOLATION")
                self.assertEqual(bridge.cp.store.last_seq, before)
            finally:
                bridge.close()

    def test_tampered_oracle_or_missing_evidence_never_accepts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = ExperimentControl(root, {"executor": "glm-5.3"}, TASK_ID)
            try:
                handle = bridge.start_role("executor", "initial")
                bridge.submit_role(handle, {"text": "fixture"})
                score = grading(root)
                score["grade_sha256"] = "0" * 64
                result = bridge.finish(score, {"fixture": True})
                self.assertFalse(result["control_accepted"])
                self.assertIsNone(result["task_pass"])
                self.assertIn("hash", result["oracle"]["reason"])
            finally:
                bridge.close()


if __name__ == "__main__":
    unittest.main()
