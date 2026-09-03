"""Offline checks for experiment validity; never invokes a model."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import run_experiment as runner


class DriverTests(unittest.TestCase):
    def test_done_inside_code_is_not_final(self):
        text = json.dumps({"type": "tool_calls", "calls": [{"name": "write", "arguments": {
            "path": "code.py", "content": "print('DONE')"}}]})
        self.assertEqual(runner.parse_action(text)["type"], "tool_calls")

    def test_protocol_and_tool_batch_boundaries(self):
        for text in ('[]', '{"type":"final","content":3}', '{"type":"tool_calls","calls":[]}',
                     json.dumps({"type": "tool_calls", "calls": [{"name": "run", "arguments": {}}]*9})):
            with self.assertRaises(ValueError):
                runner.parse_action(text)

    def test_balanced_frozen_matrix(self):
        matrix = runner.schedule()
        self.assertEqual(matrix, runner.schedule())
        self.assertEqual(len(matrix), 12)
        self.assertEqual(len({r["run_id"] for r in matrix}), 12)
        self.assertEqual(sum(runner.LIMITS[k] for k in ("planner", "executor", "verifier", "repair", "reverify")), runner.LIMITS["solo"])
        for task in runner.TASKS:
            entries = [r for r in matrix if r["task_id"] == task]
            self.assertEqual(sum(r["condition"] == "team" for r in entries), 5)

    def test_materialized_instance_matches_audited_source(self):
        audit = json.loads((runner.ROOT / "task_audit" / "audit.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="dpswarm-driver-") as temp:
            for task in audit["tasks"]:
                folder = Path(temp) / task["task_id"]
                runner.materialize(task["task_id"], folder)
                expected = {name.replace("\\", "/"): digest for name, digest in task["manifest"]["workspace_sha256"].items()}
                self.assertEqual(runner.tree_hashes(folder / "workspace"), expected)
                self.assertEqual(runner.sha(folder / "task" / "grade.sh"), task["manifest"]["grader_sha256"])
                self.assertFalse((folder / "reports" / "expected.json").exists())


if __name__ == "__main__":
    unittest.main()
