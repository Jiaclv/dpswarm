"""Offline regression tests for the seven-cell admission and completion boundary."""
import copy
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "operations" / "a2_backfill.py"
spec = importlib.util.spec_from_file_location("_test_a2_backfill", SCRIPT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class BackfillTests(unittest.TestCase):
    def auth(self):
        return {"revision": m.REVISION, "user_confirmation": "你把缺失的补上",
            "total_admission_cap": 55_800_000, "already_admitted_attempts": 86,
            "already_admitted_tokens": 51_600_000, "remaining_admission_tokens": 4_200_000,
            "new_attempt_count": 7, "new_attempt_token_admission": 600000,
            "unknown_call_count": 10, "legacy_unknown_reserved_tokens": 308099,
            "carried_original_valid_count": 73, "maximum_logical_valid": 80,
            "cost_stop_usd": 480, "cost_warning_usd": 320,
            "stage_dispatch_seconds": 219600, "package_max_seconds": 345600,
            "global_model_slots": 8, "candidate_container_cap": 12, "candidate_memory": "1g",
            "pending_run_ids": list(m.PENDING), "automatic_additional_retry": False,
            "unknown_usage_not_zero": True, "provider_limits": {"codex_account": 4, "glm_coding": 1, "deepseek": 4},
            "legacy_known_cost_usd": 123.57437509977163}

    def test_authorization_preserves_exact_scope_and_known_unknown_boundary(self):
        with patch.object(m, "read", return_value=self.auth()):
            self.assertEqual(m.authorization("unused")["new_attempt_count"], 7)
        mutations = {"total_admission_cap": 56_400_000, "already_admitted_tokens": 0,
            "pending_run_ids": m.PENDING + ["a2-01-S"], "new_attempt_count": 8,
            "new_attempt_token_admission": 300000, "unknown_call_count": 0,
            "legacy_unknown_reserved_tokens": 0, "candidate_memory": "3g",
            "provider_limits": {"codex_account": 5, "glm_coding": 1, "deepseek": 4},
            "automatic_additional_retry": True, "unknown_usage_not_zero": False,
            "user_confirmation": "那就继续啊", "legacy_known_cost_usd": 110.59}
        for key, value in mutations.items():
            with self.subTest(key=key):
                auth = self.auth()
                auth[key] = value
                with patch.object(m, "read", return_value=auth), self.assertRaises(Exception):
                    m.authorization("unused")

    def test_prefix_does_not_refund_an_interrupted_admission(self):
        with tempfile.TemporaryDirectory() as folder:
            state = {"completed_episodes": 0, "episodes": [], "token_admission_sum": 52_200_000,
                     "known_cost_usd": 123.57437509977163}
            data = ({"legacy_known_cost_usd": state["known_cost_usd"]}, {"schedule": []}, state)
            with patch.object(m, "contract", return_value=data), self.assertRaisesRegex(Exception, "refunded"):
                m.prefix_audit(Path(folder), object())

    def test_original_deadline_uses_earlier_stage_or_package_limit(self):
        start = datetime(2026, 9, 5, tzinfo=timezone.utc)
        data = {"stage_started_at": start.isoformat(), "package_started_at": (start-timedelta(hours=50)).isoformat()}
        self.assertEqual(m.base.deadline(data), start + timedelta(hours=46))

    def wave_fixture(self, root):
        batch, group = root / "batch", root / "ops" / "wave-001-007"
        batch.mkdir()
        group.mkdir(parents=True)
        start = datetime.now(timezone.utc)
        value = {"stage_started_at": start.isoformat(), "package_started_at": start.isoformat(),
                 "total_admission_cap": m.TOTAL_CAP, "cost_stop_usd": 480}
        state = {"token_admission_sum": m.PRIOR_ADMISSION, "started_at": start.isoformat(),
            "parallel_transition": {"status": "ready", "transition_id": group.name,
                                    "record": str(group / "transition.json")}}
        for name in ("manifest.json", "recovery-plan.json", "state.json"):
            m.write(batch / name, {})
        plan = {"run_dir": str(group.parent), "manifest_sha256": m.sha(batch / "manifest.json"),
                "recovery_plan_sha256": m.sha(batch / "recovery-plan.json"),
                "package_started_at": start.isoformat(), "deadline": (start+timedelta(hours=61)).isoformat()}
        policy = m.base.policy_for(plan, list(m.PENDING))
        m.write(group / "policy.json", policy)
        m.write(group / "transition.json", {"recovery_plan_sha256": plan["recovery_plan_sha256"], "previous_group": None})
        manifest = {"resource_lock_path": str(root / "stage.lease"), "schedule": [{"run_id": rid} for rid in m.PENDING]}
        return batch, group, value, manifest, state, policy

    def test_wave_accepts_only_seven_cells_at_real_558m_cap(self):
        with tempfile.TemporaryDirectory() as folder:
            batch, group, value, manifest, state, policy = self.wave_fixture(Path(folder))
            data = (value, manifest, state, [], 123.57437509977163)
            with patch.object(m, "prefix_audit", return_value=data), \
                 patch.object(m.controller, "candidate_requirements", return_value={rid: 1 for rid in m.PENDING}), \
                 patch.object(m.controller, "profile_binding", return_value={}):
                result = m.inspect_wave(batch, group, cli=object())
                self.assertEqual(result["run_ids"], m.PENDING)
                self.assertEqual(result["prior_token_admission_sum"] + 7*600000, 55_800_000)
                value["total_admission_cap"] = 51_600_000
                with self.assertRaisesRegex(Exception, "admission or cost"):
                    m.inspect_wave(batch, group, cli=object())
                value["total_admission_cap"] = 55_800_000
                policy["run_ids"] = m.PENDING[:-1]
                m.write(group / "policy.json", policy)
                with self.assertRaisesRegex(Exception, "seven-attempt"):
                    m.inspect_wave(batch, group, cli=object())

    def run_supervisor(self, terminal):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            plan = {"batch": str(root / "batch"), "run_dir": str(root / "ops"), "deadline": "unchanged", "legacy_unknown_calls": []}
            (root / "ops").mkdir()
            calls = {"prepare": 0, "launch": 0, "abort": 0}
            def prepare(*args):
                calls["prepare"] += 1
                return root / "ops" / "wave-001-007"
            def launch(*args):
                calls["launch"] += 1
                return {"pid": 1}
            def wait(*args):
                if isinstance(terminal, Exception):
                    raise terminal
                return terminal
            def abort(*args, **kwargs):
                calls["abort"] += 1
                return {"confirmed": True}
            with patch.object(m.cont, "continuation_lease", return_value=nullcontext()), \
                 patch.object(m.base, "verify_supervisor"):
                result = m.supervise(plan, cli=object(), preparer=prepare, launcher=launch, waiter=wait, aborter=abort)
            return result, calls

    def test_success_is_exactly_one_wave_and_80_valid_results(self):
        result, calls = self.run_supervisor({"new_valid_episodes": 7, "cleanup_confirmed": True})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["logical_valid_total"], 80)
        self.assertEqual(result["total_admitted_attempts"], 93)
        self.assertEqual(calls, {"prepare": 1, "launch": 1, "abort": 0})

    def test_failure_or_incomplete_score_stops_without_automatic_retry(self):
        for terminal in (RuntimeError("provider unavailable"), {"new_valid_episodes": 6, "cleanup_confirmed": True},
                         {"new_valid_episodes": 7, "cleanup_confirmed": False}):
            with self.subTest(terminal=terminal):
                result, calls = self.run_supervisor(terminal)
                self.assertEqual(result["status"], "stopped")
                self.assertNotIn("original_matrix_complete", result)
                self.assertEqual(calls, {"prepare": 1, "launch": 1, "abort": 1})
                self.assertTrue(result["failure_cleanup"]["confirmed"])


if __name__ == "__main__":
    unittest.main()
