"""Read-only evidence audit; no model, grader, Docker, or ControlPlane execution.

Default writes only FINAL_AUDIT.json. --check-only prints the same audit summary
without writing files. Exit codes: 0 pass, 1 confirmed failure, 2 incomplete,
3 unknown. A task's raw pass=false is not an evidence-audit failure.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any


DEFAULT_ROOT = Path(__file__).resolve().parent
TERMINAL = {"scored", "transport_error", "grader_error", "infrastructure_error"}
TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens")
USAGE_FIELDS = TOKEN_FIELDS + ("cached_input_tokens", "reasoning_tokens")


def obj(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def integer(value: Any) -> bool:
    return type(value) is int and value >= 0


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def token_sums(calls: list[dict]) -> dict:
    """Unknown call usage remains null; a known subtotal is explicitly separate."""
    out = {"calls": len(calls)}
    for field in USAGE_FIELDS:
        values = [c.get(field) for c in calls]
        known = [v for v in values if integer(v)]
        out[field] = {
            "value": sum(known) if len(known) == len(values) else None,
            "known_subtotal": sum(known) if known else (0 if not values else None),
            "known_calls": len(known), "unknown_calls": len(values) - len(known),
        }
    return out


class Audit:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.checks: list[dict] = []
        self.read_stats: dict[Path, tuple[int, int]] = {}
        self.runs: list[dict] = []
        self.manifest: dict = {}
        self.all_terminal = False

    def add(self, code: str, location: str, status: str, **details: Any) -> None:
        self.checks.append({"code": code, "location": location, "status": status, **details})

    def eq(self, code: str, location: str, actual: Any, expected: Any) -> None:
        if actual is None or expected is None:
            self.add(code, location, "unknown", actual=actual, expected=expected)
        else:
            # Python considers True == 1; these schemas distinguish booleans.
            equal = actual == expected and (not isinstance(expected, bool) or type(actual) is bool)
            self.add(code, location, "pass" if equal else "fail", actual=actual, expected=expected)

    def require(self, code: str, location: str, value: bool, **details: Any) -> None:
        self.add(code, location, "pass" if value else "fail", **details)

    def read(self, path: Path, missing: str = "unknown") -> Any:
        try:
            stat = path.stat()
            data = path.read_bytes()
            self.read_stats[path] = (stat.st_size, stat.st_mtime_ns)
            return json.loads(data.decode("utf-8-sig"))
        except FileNotFoundError:
            self.add("artifact_read", str(path), missing, reason="missing")
        except (OSError, UnicodeError, ValueError) as exc:
            self.add("artifact_read", str(path), missing, reason=str(exc))
        return None

    def inside(self, base: Path, relative: str) -> Path | None:
        try:
            path = (base / relative).resolve()
            if not path.is_relative_to(base.resolve()):
                raise ValueError("path escapes declared base")
            return path
        except (OSError, ValueError, TypeError) as exc:
            self.add("path_boundary", str(base), "fail", path=relative, reason=str(exc))
            return None

    def hash_file(self, path: Path, expected: Any, code: str) -> None:
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            self.add(code, str(path), "unknown", reason="missing or invalid declared sha256", expected=expected)
            return
        try:
            self.eq(code, str(path), sha256(path), expected)
        except OSError as exc:
            self.add(code, str(path), "fail", reason=str(exc), expected=expected)

    def hashes(self, name: str, mapping: Any, base: Path, exact: bool = False) -> None:
        if not isinstance(mapping, dict) or not mapping:
            self.add(name, str(base), "unknown", reason="hash map absent or empty")
            return
        for relative, digest in mapping.items():
            path = self.inside(base, relative)
            if path is not None:
                self.hash_file(path, digest, name)
        if exact:
            try:
                actual = sorted(p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file())
                self.eq(name + "_file_set", str(base), actual, sorted(mapping))
            except OSError as exc:
                self.add(name + "_file_set", str(base), "unknown", reason=str(exc))

    def freeze(self) -> None:
        native = self.manifest.get("experiment_kind") == "native_tools_followup"
        program_base = self.root.parent if native else self.root
        core_base = program_base.parents[1]
        self.bases = {"program": str(program_base), "core": str(core_base), "instances": str(self.root / "instances")}
        self.hashes("program_hash", self.manifest.get("program_sha256"), program_base)
        self.hashes("core_hash", self.manifest.get("control_core_sha256"), core_base)
        for task, hashes in obj(self.manifest.get("instances")).items():
            base = self.inside(self.root / "instances", task)
            if base is not None:
                self.hashes("instance_hash", hashes, base, exact=True)
        if not obj(self.manifest.get("instances")):
            self.add("instance_hash", str(self.root), "unknown", reason="no frozen instance hashes")
        if native:
            self.hash_file(program_base / "manifest.json", self.manifest.get("main_manifest_sha256"), "main_manifest_hash")
            self.hashes("main_program_hash", self.manifest.get("main_program_sha256"), program_base)
            for task, hashes in obj(self.manifest.get("main_instances")).items():
                base = self.inside(program_base / "instances", task)
                if base is not None:
                    self.hashes("main_instance_hash", hashes, base, exact=True)

    def ledger(self) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
        started: dict[str, list[dict]] = defaultdict(list)
        completed: dict[str, list[dict]] = defaultdict(list)
        path = self.root / "calls.jsonl"
        try:
            stat = path.stat()
            content = path.read_bytes().decode("utf-8-sig")
            self.read_stats[path] = (stat.st_size, stat.st_mtime_ns)
        except FileNotFoundError:
            self.add("ledger_read", str(path), "fail" if self.all_terminal else "pending", reason="missing")
            return started, completed
        except (OSError, UnicodeError) as exc:
            self.add("ledger_read", str(path), "fail" if self.all_terminal else "pending", reason=str(exc))
            return started, completed
        for number, line in enumerate(content.splitlines(), 1):
            if not line.strip():
                continue
            location = f"{path}:{number}"
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("ledger event is not an object")
                call_id = event.get("call_id")
                if not isinstance(call_id, str) or not call_id:
                    raise ValueError("missing call_id")
                target = {"started": started, "completed": completed}.get(event.get("event"))
                if target is None:
                    raise ValueError("unsupported ledger event")
                target[call_id].append(event)
            except (ValueError, TypeError) as exc:
                self.add("ledger_event", location, "fail" if self.all_terminal else "pending", reason=str(exc))
        return started, completed

    def usage_check(self, calls: list[dict], result: dict, location: str) -> dict:
        sums = token_sums(calls)
        for field in TOKEN_FIELDS:
            self.eq("run_usage_sum", location + ":" + field, result.get(field), sums[field]["value"])
        known_io = all(integer(c.get(f)) for c in calls for f in TOKEN_FIELDS[:2])
        self.eq("usage_complete_claim", location, result.get("usage_complete"), known_io)
        total = obj(obj(result.get("control")).get("usage")).get("total")
        if isinstance(total, dict):
            for field in TOKEN_FIELDS:
                self.eq("control_usage_sum", location + ":" + field, total.get(field), sums[field]["value"])
        return sums

    def phases(self, run_path: Path, result: dict, calls: dict[str, dict]) -> dict:
        phases = result.get("phases")
        location = result.get("run_id", str(run_path))
        if not isinstance(phases, list):
            self.add("phases_present", location, "unknown")
            return {"protocol_errors": None, "tool_calls": None}
        ids: list[str] = []
        phase_names: list[str] = []
        protocol_count, tool_count = 0, 0
        for phase in phases:
            if not isinstance(phase, dict) or not isinstance(phase.get("phase"), str):
                self.add("phase_schema", location, "fail")
                continue
            name = phase["phase"]
            phase_names.append(name)
            phase_path = self.inside(run_path / "phases", name)
            if phase_path is None:
                continue
            self.eq("phase_artifact", str(phase_path), self.read(phase_path / "phase.json"), phase)
            phase_ids = phase.get("call_ids")
            if not isinstance(phase_ids, list) or any(not isinstance(x, str) for x in phase_ids):
                self.add("phase_call_ids", str(phase_path), "fail")
                phase_ids = []
            ids.extend(phase_ids)
            limit = obj(self.manifest.get("limits")).get(name)
            if integer(limit):
                self.require("phase_call_limit", str(phase_path), len(phase_ids) <= limit, actual=len(phase_ids), limit=limit)
            else:
                self.add("phase_call_limit", str(phase_path), "unknown", limit=limit)
            for call_id in phase_ids:
                if call_id in calls:
                    self.eq("phase_call_role", call_id, calls[call_id].get("role"), phase.get("role"))
                    self.eq("phase_call_model", call_id, calls[call_id].get("model_requested"), phase.get("model"))
            steps = []
            for path in sorted(phase_path.glob("turn_*.json")):
                step = self.read(path)
                if not isinstance(step, dict):
                    continue
                steps.append(step)
                call_id = step.get("call_id")
                self.require("turn_call_membership", str(path), call_id in phase_ids, call_id=call_id)
                if call_id in phase_ids:
                    self.eq("turn_index", str(path), step.get("turn"), phase_ids.index(call_id) + 1)
            step_ids = [s.get("call_id") for s in steps]
            self.require("turn_call_unique", str(phase_path), len(step_ids) == len(set(step_ids)))
            for call_id in phase_ids:
                if call_id in calls and not calls[call_id].get("error"):
                    self.require("completed_call_has_turn", str(phase_path), call_id in step_ids, call_id=call_id)
            observed_protocol = sum("protocol_error" in step for step in steps)
            observed_tools = sum(len(s["tool_results"]) for s in steps if isinstance(s.get("tool_results"), list))
            self.eq("phase_protocol_errors", str(phase_path), phase.get("protocol_errors"), observed_protocol)
            self.eq("phase_tool_calls", str(phase_path), phase.get("tool_calls"), observed_tools)
            protocol_count += observed_protocol
            tool_count += observed_tools
        self.require("phase_names_unique", location, len(phase_names) == len(set(phase_names)))
        self.eq("phase_call_ids", location, ids, result.get("call_ids"))
        actual_names = sorted(p.name for p in (run_path / "phases").iterdir() if p.is_dir()) if (run_path / "phases").is_dir() else []
        self.eq("phase_directory_set", location, sorted(phase_names), actual_names)
        return {"protocol_errors": protocol_count, "tool_calls": tool_count}

    def score(self, run_path: Path, result: dict) -> None:
        location = result["run_id"]
        grading = obj(result.get("grading"))
        declared_raw = grading.get("raw_score_path")
        if not isinstance(declared_raw, str):
            self.add("raw_score_path", location, "unknown")
            return
        # Absolute paths may be declared, but must resolve within this run.
        raw_path = self.inside(run_path, declared_raw)
        if raw_path is None:
            return
        raw = self.read(raw_path, missing="fail")
        self.eq("raw_equals_result_score", location, result.get("score"), raw)
        self.eq("raw_equals_grading_raw", location, grading.get("raw_score"), raw)
        self.eq("raw_equals_grading_score", location, grading.get("score"), raw)
        if result.get("status") == "scored":
            self.eq("scored_grader_exit", location, grading.get("exit_code"), 0)
            self.eq("scored_grader_timeout", location, grading.get("timed_out"), False)
            self.require("scored_parse_error_absent", location, "score_parse_error" in grading and grading["score_parse_error"] is None)
        expected_grade = obj(obj(self.manifest.get("instances")).get(result.get("task_id"))).get("task/grade.sh")
        self.eq("grading_hash_matches_instance", location, grading.get("grade_sha256"), expected_grade)
        self.hash_file(run_path / "task" / "grade.sh", expected_grade, "run_grade_hash")
        control = obj(result.get("control"))
        self.eq("control_artifact", location, self.read(run_path / "control-plane" / "result.json"), control)
        self.require("control_error_absent", location, not control.get("error"), error=control.get("error"))
        oracle = obj(control.get("oracle"))
        raw_pass = obj(raw).get("pass")
        self.require("raw_pass_boolean", location, type(raw_pass) is bool, actual=raw_pass)
        self.eq("oracle_verified_evidence", location, oracle.get("verified"), True)
        self.eq("replay_passed_evidence", location, control.get("invariant_replay_passed"), True)
        self.eq("oracle_raw_score", location, oracle.get("raw_score"), raw)
        self.eq("oracle_pass", location, oracle.get("pass"), raw_pass)
        self.eq("control_task_pass", location, control.get("task_pass"), raw_pass)
        self.eq("oracle_grade_hash", location, oracle.get("grade_sha256"), expected_grade)
        self.eq("oracle_canonical_grade_hash", location, oracle.get("canonical_grade_sha256"), expected_grade)
        self.eq("oracle_source_commit", location, oracle.get("source_commit"), self.manifest.get("source_commit"))
        if raw_path.is_file():
            self.eq("oracle_score_hash", location, oracle.get("score_sha256"), sha256(raw_path))
        oracle_path = oracle.get("score_path")
        self.eq("oracle_score_path", location, str(Path(oracle_path).resolve()) if isinstance(oracle_path, str) else None, str(raw_path))
        factors = [oracle.get("verified"), raw_pass, control.get("role_evidence_complete")]
        accepted = all(factors) if all(type(v) is bool for v in factors) else None
        self.eq("control_accepted_rule", location, control.get("control_accepted"), accepted)
        journal = control.get("journal_path")
        journal_path = self.inside(run_path, journal) if isinstance(journal, str) else None
        if journal_path is None:
            self.add("control_journal_evidence", location, "unknown", reason="no journal path")
        else:
            try:
                records = [json.loads(line) for line in journal_path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
                self.require("control_journal_evidence", location, bool(records) and all(isinstance(r, dict) for r in records), records=len(records))
            except (OSError, UnicodeError, ValueError) as exc:
                self.add("control_journal_evidence", location, "fail", reason=str(exc))

    def cleanup(self, run_path: Path, result: dict) -> None:
        for field in ("control_cleanup_error", "sandbox_cleanup_error"):
            self.require("cleanup_error_absent", result["run_id"] + ":" + field, not result.get(field), error=result.get(field))
        cleanup = self.read(run_path / "sandbox" / "cleanup.json")
        self.eq("sandbox_cleanup_errors", result["run_id"], obj(cleanup).get("errors"), [])
        containers = self.read(run_path / "sandbox" / "containers.json")
        self.eq("sandbox_closed_evidence", result["run_id"], obj(containers).get("closed"), True)
        self.eq("sandbox_image_evidence", result["run_id"], obj(containers).get("image"), self.manifest.get("image_id"))

    def run(self) -> dict:
        self.manifest = obj(self.read(self.root / "manifest.json", missing="fail"))
        schedule = self.manifest.get("schedule")
        if not isinstance(schedule, list) or not schedule or any(not isinstance(e, dict) or not isinstance(e.get("run_id"), str) for e in schedule):
            self.add("schedule_schema", str(self.root), "fail", reason="nonempty schedule required")
            return self.finish([], {}, {})
        scheduled_ids = [e["run_id"] for e in schedule]
        self.require("schedule_unique", str(self.root), len(scheduled_ids) == len(set(scheduled_ids)))
        results: dict[str, dict] = {}
        for entry in schedule:
            run_path = self.inside(self.root / "results", entry["run_id"])
            if run_path is None:
                continue
            result = self.read(run_path / "result.json", missing="pending")
            if isinstance(result, dict):
                results[entry["run_id"]] = result
        terminal_ids = {k for k, r in results.items() if r.get("status") in TERMINAL and isinstance(r.get("completed_at"), str) and r["completed_at"]}
        self.all_terminal = len(terminal_ids) == len(schedule)
        for run_id in scheduled_ids:
            self.add("run_terminated", run_id, "pass" if run_id in terminal_ids else "pending", run_status=obj(results.get(run_id)).get("status"))
        extra_results = sorted(p.parent.name for p in (self.root / "results").glob("*/result.json") if p.parent.name not in scheduled_ids)
        self.require("result_schedule_membership", str(self.root), not extra_results, unexpected_run_ids=extra_results)
        self.freeze()
        started, completed = self.ledger()
        completed_calls: dict[str, dict] = {}
        by_run: dict[str, list[dict]] = defaultdict(list)
        initiated_by_run: dict[str, set[str]] = defaultdict(set)
        for call_id in sorted(set(started) | set(completed)):
            starts, ends = started.get(call_id, []), completed.get(call_id, [])
            records = starts + ends
            for event in records:
                run_id = event.get("run_id")
                self.require("call_run_scheduled", call_id, run_id in scheduled_ids, run_id=run_id)
                if event.get("event") == "started":
                    initiated_by_run[str(run_id)].add(call_id)
            self.require("call_started_unique", call_id, len(starts) <= 1, count=len(starts))
            self.require("call_completed_unique", call_id, len(ends) <= 1, count=len(ends))
            if len(starts) != 1 or len(ends) != 1:
                run_id = records[0].get("run_id")
                status = "fail" if ends or run_id in terminal_ids else "pending"
                self.add("call_pair", call_id, status, started=len(starts), completed=len(ends), run_id=run_id)
            else:
                self.add("call_pair", call_id, "pass")
                for field in ("run_id", "task_id", "role", "model_requested", "started_at"):
                    self.eq("call_event_identity", call_id + ":" + field, ends[0].get(field), starts[0].get(field))
            # Duplicate completions are never silently deduplicated into a sum.
            if len(ends) != 1:
                continue
            call = ends[0]
            completed_calls[call_id] = call
            by_run[str(call.get("run_id"))].append(call)
            for field in USAGE_FIELDS:
                value = call.get(field)
                if value is not None:
                    self.require("token_nonnegative_integer", call_id + ":" + field, integer(value), actual=value)
            components = [call.get(f) for f in TOKEN_FIELDS]
            if all(integer(v) for v in components):
                self.eq("call_total_equals_io", call_id, components[2], components[0] + components[1])
            else:
                self.add("call_total_equals_io", call_id, "unknown", input_tokens=components[0], output_tokens=components[1], total_tokens=components[2])
        for entry in schedule:
            run_id = entry["run_id"]
            result = results.get(run_id)
            calls = by_run.get(run_id, [])
            count = len(initiated_by_run.get(run_id, set()))
            self.require("run_call_limit_18", run_id, count <= 18, started_calls=count, limit=18)
            summary = {**entry, "terminated": run_id in terminal_ids, "status": obj(result).get("status"), "started_calls": count, "completed_calls": len(calls), "usage": token_sums(calls)}
            self.runs.append(summary)
            if result is None or run_id not in terminal_ids:
                continue
            run_path = self.root / "results" / run_id
            for field in ("run_id", "task_id", "condition", "executor"):
                self.eq("run_schedule_identity", run_id + ":" + field, result.get(field), entry.get(field))
            for call in calls:
                self.eq("call_task_identity", call["call_id"], call.get("task_id"), entry.get("task_id"))
            ids = result.get("call_ids")
            if isinstance(ids, list) and all(isinstance(v, str) for v in ids):
                self.require("result_call_ids_unique", run_id, len(ids) == len(set(ids)))
                self.eq("result_call_ids_match_ledger", run_id, sorted(ids), sorted(c["call_id"] for c in calls))
                self.eq("result_call_count", run_id, result.get("call_count"), len(ids))
                self.require("result_call_limit_18", run_id, len(ids) <= 18, actual=len(ids), limit=18)
            else:
                self.add("result_call_ids_schema", run_id, "fail")
            summary["usage"] = self.usage_check(calls, result, run_id)
            summary.update(self.phases(run_path, result, completed_calls))
            if result.get("status") == "scored" or obj(result.get("grading")).get("raw_score_path"):
                self.score(run_path, result)
            else:
                self.add("grading_applicability", run_id, "not_applicable", reason="terminal non-scored run has no grading artifact")
            self.cleanup(run_path, result)
        return self.finish(schedule, results, completed_calls)

    def finish(self, schedule: list[dict], results: dict, calls: dict) -> dict:
        # A moving ledger/result is a snapshot, never a final passing audit.
        for path, before in self.read_stats.items():
            try:
                stat = path.stat()
                after = (stat.st_size, stat.st_mtime_ns)
            except OSError:
                after = None
            if after != before:
                self.add("snapshot_changed", str(path), "pending", before=before, after=after)
        counts = Counter(c["status"] for c in self.checks)
        status = ("incomplete" if not self.all_terminal or counts["pending"] else
                  "fail" if counts["fail"] else "unknown" if counts["unknown"] else "pass")
        usage = token_sums(list(calls.values()))
        # Counts/sums describe only uniquely completed ledger entries, not calls
        # without a completion or rejected duplicate completions.
        return {
            "schema_version": 1, "root": str(self.root),
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "experiment_kind": self.manifest.get("experiment_kind", "main_matrix"),
            "experiment_label": self.manifest.get("experiment_label"),
            "status": status,
            "audit_pass": False if counts["fail"] else (True if status == "pass" else None),
            "completion": {"scheduled": len(schedule), "terminal": sum(r.get("status") in TERMINAL and bool(r.get("completed_at")) for r in results.values()), "all_terminal": self.all_terminal},
            "check_counts": {s: counts[s] for s in ("pass", "fail", "unknown", "pending", "not_applicable")},
            "frozen_hash_bases": getattr(self, "bases", None),
            "usage_unique_completed_calls": usage,
            "limits": {
                "audit_scope": "Only this root's scheduled scored/failed runs; unscored preflight excluded. Native follow-up is separate from main scores.",
                "control_replay": "Saved verified/replay claims, raw files and journal consistency only; no independent replay or core test executed.",
                "cleanup": "Saved cleanup evidence only; no live container inspection.",
                "usage": "Null is unknown; known_subtotal is not a complete total. Cached/reasoning tokens are subdivisions, not added to input+output.",
                "telemetry": "No provider truth, actual service tier, cost, or orchestration/analysis-agent usage inferred.",
                "task_pass": "A raw task failure or terminal transport error does not by itself fail this consistency audit.",
            },
            "runs": self.runs, "checks": self.checks,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--check-only", action="store_true", help="Read and print summary without writing FINAL_AUDIT.json")
    args = parser.parse_args()
    audit = Audit(args.root)
    try:
        report = audit.run()
    except Exception as exc:
        # Preserve a concrete verifier failure rather than printing a false pass.
        audit.add("audit_internal_error", str(audit.root), "fail", error_type=type(exc).__name__, reason=str(exc))
        report = audit.finish([], {}, {})
    if not args.check_only:
        (audit.root / "FINAL_AUDIT.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("status", "audit_pass", "completion", "check_counts")}, ensure_ascii=False))
    failures = [c for c in report["checks"] if c["status"] == "fail"]
    for check in failures[:6]:
        print(json.dumps(check, ensure_ascii=False))
    if len(failures) > 6:
        print(f"Additional failures omitted from stdout: {len(failures) - 6}; see FINAL_AUDIT.json when written.")
    return {"pass": 0, "fail": 1, "incomplete": 2, "unknown": 3}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
