"""Real Docker permission preflight; no model requests are made.

Run ``python -X utf8 test_sandbox.py --preflight`` after ``python sandbox.py --build``.
Creates an isolated seed-0 DIST1 baseline from the pinned upstream generator.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import traceback
import uuid

from sandbox import DEFAULT_IMAGE, RoleSandbox, _call, _utc


def materialize_baseline(vendor: Path, run_dir: Path) -> Path:
    sys.path.insert(0, str(vendor))
    from generators.registry import get_generator
    generated = get_generator("DIST1_queue_race").generate(0)
    task_dir = run_dir / "task"
    task_dir.mkdir(parents=True)
    (task_dir / "spec.md").write_text(generated.spec_md, encoding="utf-8")
    (task_dir / "brief.md").write_text(generated.brief_md, encoding="utf-8")
    source_task = vendor / "tasks" / "DIST1_queue_race"
    # Windows checkout converts tracked shell scripts to CRLF; the canonical
    # untouched Git blob preserves the upstream LF bytes that bash requires.
    grader = subprocess.run(["git", "-C", str(vendor), "show", "HEAD:tasks/DIST1_queue_race/grade.sh"],
                            capture_output=True, check=True).stdout
    (task_dir / "grade.sh").write_bytes(grader)
    shutil.copyfile(source_task / "task.yaml", task_dir / "task.yaml")
    workspace = run_dir / "workspace"
    workspace.mkdir()
    for relative, content in generated.workspace_files.items():
        target = (workspace / relative).resolve()
        if not target.is_relative_to(workspace.resolve()):
            raise ValueError("Generated path escaped the workspace")
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8", newline="\n")
    (run_dir / "grader").mkdir()
    (run_dir / "grader" / "expected.json").write_text(
        json.dumps(generated.expected, indent=2), encoding="utf-8")
    return task_dir


def preflight(directory: Path, image: str = DEFAULT_IMAGE) -> dict:
    vendor = directory / "TeamBench"
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "_" + uuid.uuid4().hex[:8]
    run_dir = directory / "preflight" / stamp
    task_dir = materialize_baseline(vendor, run_dir)
    commit = _call(["git", "-C", str(vendor), "rev-parse", "HEAD"]).stdout.strip()
    evidence = {"started_at": _utc(), "task_id": "DIST1_queue_race", "seed": 0,
                "purpose": "OS isolation and buggy-baseline grader preflight; not a model benchmark run",
                "model_requests": 0, "vendor_commit": commit, "image": image,
                "run_dir": str(run_dir), "checks": [], "passed": False}
    sandbox = RoleSandbox(task_dir, run_dir, image)

    def check(label: str, passed: bool, detail=None) -> None:
        evidence["checks"].append({"name": label, "passed": bool(passed), "detail": detail})
        if not passed:
            raise AssertionError(label + ": " + str(detail))

    try:
        sandbox.start()
        details = json.loads((sandbox.logs / "roles.inspect.json").read_text(encoding="utf-8"))
        for role, item in zip(("planner", "executor", "verifier", "oracle"), details):
            host = item["HostConfig"]
            check(role + "_hardened", item["Config"]["User"] == "10001:10001"
                  and host["NetworkMode"] == "none" and host["ReadonlyRootfs"]
                  and "ALL" in host["CapDrop"] and host["Memory"] == 1073741824
                  and host["NanoCpus"] == 2000000000 and host["PidsLimit"] == 128
                  and any(v.startswith("no-new-privileges") for v in host["SecurityOpt"]),
                  {"User": item["Config"]["User"], "NetworkMode": host["NetworkMode"],
                   "ReadonlyRootfs": host["ReadonlyRootfs"], "Memory": host["Memory"],
                   "NanoCpus": host["NanoCpus"], "PidsLimit": host["PidsLimit"],
                   "CapDrop": host["CapDrop"], "SecurityOpt": host["SecurityOpt"]})
            mounts = item["Mounts"]
            check(role + "_no_sensitive_mounts", all("docker.sock" not in mount["Source"]
                and "/grader" != mount["Destination"] for mount in mounts), mounts)
        result = sandbox.tool("planner", "read", {"path": "/task/spec.md"})
        check("planner_reads_full_spec", result["exit_code"] == 0 and bool(result["stdout"]), result)
        result = sandbox.tool("planner", "run", {"cmd": "id"})
        check("planner_run_denied", result["exit_code"] != 0, result)
        result = sandbox.tool("planner", "write", {"path": "/workspace/probe.txt", "content": "bad"})
        check("planner_write_denied", result["exit_code"] != 0, result)
        for name, args in (("read", {"path": "/task/spec.md"}),
                           ("run", {"cmd": "cat /task/spec.md"})):
            result = sandbox.tool("executor", name, args)
            check("executor_spec_denied_" + name, result["exit_code"] != 0, result)
        result = sandbox.tool("executor", "read", {"path": "/task/brief.md"})
        check("executor_reads_brief", result["exit_code"] == 0 and bool(result["stdout"]), result)
        for role in ("executor", "verifier", "oracle"):
            result = sandbox.tool(role, "run", {"cmd": "cat /grader/expected.json /reports/expected.json"})
            check(role + "_ground_truth_not_mounted", result["exit_code"] != 0, result)
        result = sandbox.tool("executor", "write", {"path": "sandbox_probe.txt", "content": "original"})
        check("executor_writes_relative_workspace", result["exit_code"] == 0
              and (sandbox.workspace / "sandbox_probe.txt").read_text() == "original", result)
        result = sandbox.tool("verifier", "read", {"path": "/workspace/sandbox_probe.txt"})
        check("workspace_alias_reads", result["exit_code"] == 0 and result["stdout"] == "original", result)
        for name, args in (("write", {"path": "/workspace/sandbox_probe.txt", "content": "bad"}),
                           ("run", {"cmd": "printf bad >> /workspace/sandbox_probe.txt"})):
            result = sandbox.tool("verifier", name, args)
            check("verifier_workspace_write_denied_" + name, result["exit_code"] != 0
                  and (sandbox.workspace / "sandbox_probe.txt").read_text() == "original", result)
        result = sandbox.tool("verifier", "run", {"cmd": "touch /reports/sandbox_probe.txt"})
        check("verifier_reports_readonly", result["exit_code"] != 0, result)
        result = sandbox.tool("verifier", "write", {"path": "sandbox_probe.json", "content": "{}"})
        check("verifier_relative_write_is_submission", result["exit_code"] == 0
              and (sandbox.submission / "sandbox_probe.json").read_text() == "{}", result)
        result = sandbox.tool("executor", "run", {"cmd": "ln -s /etc/passwd /workspace/sandbox_escape"})
        check("symlink_probe_created", result["exit_code"] == 0, result)
        result = sandbox.tool("executor", "read", {"path": "sandbox_escape"})
        check("read_symlink_escape_denied", result["exit_code"] != 0, result)
        result = sandbox.tool("executor", "write", {"path": "sandbox_escape", "content": "bad"})
        check("write_symlink_escape_denied", result["exit_code"] != 0, result)
        result = sandbox.tool("executor", "read", {"path": "/shared/workspace/../../etc/passwd"})
        check("read_traversal_denied", result["exit_code"] != 0, result)
        result = sandbox.tool("oracle", "read", {"path": "/task/spec.md"})
        check("oracle_fullspec", result["exit_code"] == 0, result)
        result = sandbox.tool("oracle", "write", {"path": "sandbox_oracle.txt", "content": "oracle"})
        check("oracle_workspace_writable", result["exit_code"] == 0, result)
        result = sandbox.tool("executor", "run", {"cmd": "id -u; ls /sys/class/net; python -m pytest --version; python -c 'import pytest_timeout, yaml'"})
        check("runtime_dependencies_and_uid", result["exit_code"] == 0 and "10001\nlo\n" in result["stdout"]
              and "pytest 8.4.2" in result["stdout"], result)
        result = sandbox.tool("executor", "run", {"cmd": "rm -- sandbox_probe.txt sandbox_escape sandbox_oracle.txt"})
        check("workspace_probes_removed", result["exit_code"] == 0, result)
        result = sandbox.tool("verifier", "run", {"cmd": "rm -- /submission/sandbox_probe.json"})
        check("submission_probe_removed", result["exit_code"] == 0, result)
        grade = sandbox.grade()
        evidence["grade"] = grade
        frozen = json.loads((sandbox.logs / "grade" / "frozen_roles.json").read_text(encoding="utf-8"))
        check("candidate_processes_frozen_before_grade", sorted(frozen) == ["executor", "oracle", "planner", "verifier"], frozen)
        raw = grade["raw_score"]
        check("official_grader_produces_baseline_score", grade["exit_code"] == 0 and not grade["timed_out"]
              and isinstance(raw, dict) and raw.get("pass") is False
              and raw.get("secondary", {}).get("total_checks") == 12, grade)
        upstream_blob = subprocess.run(["git", "-C", str(vendor), "show", "HEAD:tasks/DIST1_queue_race/grade.sh"],
                                       capture_output=True, check=True).stdout
        upstream_hash = hashlib.sha256(upstream_blob).hexdigest()
        check("grade_script_unchanged", grade["grade_sha256"] == upstream_hash, upstream_hash)
        evidence["passed"] = True
    except Exception as exc:
        evidence["error"] = repr(exc)
        evidence["traceback"] = traceback.format_exc()
    finally:
        try:
            sandbox.close()
            evidence["cleanup"] = json.loads((sandbox.logs / "cleanup.json").read_text(encoding="utf-8"))
        except Exception as exc:
            evidence["cleanup_error"] = repr(exc)
            evidence["passed"] = False
        evidence["finished_at"] = _utc()
        (directory / "sandbox_preflight.json").write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8")
    return evidence


def freeze_check(directory: Path, image: str = DEFAULT_IMAGE) -> dict:
    """Targeted regression for explicit final freeze followed by grade()."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "_" + uuid.uuid4().hex[:8]
    run_dir = directory / "preflight" / ("freeze_" + stamp)
    task_dir = materialize_baseline(directory / "TeamBench", run_dir)
    box = RoleSandbox(task_dir, run_dir, image)
    evidence = {"started_at": _utc(), "passed": False, "checks": [], "model_requests": 0,
                "run_dir": str(run_dir), "purpose": "Targeted freeze/grade regression only"}
    try:
        box.start()
        first = box.freeze()
        second = box.freeze()
        assert sorted(first) == ["executor", "oracle", "planner", "verifier"] and first == second
        evidence["checks"].append("freeze_is_idempotent")
        inspected = json.loads(_call(["docker", "inspect", *box.containers.values()]).stdout)
        assert all(item["State"]["Paused"] for item in inspected)
        evidence["checks"].append("all_candidate_containers_are_paused")
        before = (box.workspace / "sandbox_frozen_write.txt").exists()
        result = box.tool("executor", "write", {"path": "sandbox_frozen_write.txt", "content": "bad"})
        assert result["exit_code"] != 0 and "frozen" in result["stderr"]
        assert (box.workspace / "sandbox_frozen_write.txt").exists() == before
        evidence["checks"].append("tools_rejected_after_freeze")
        evidence["grade"] = box.grade()
        assert evidence["grade"]["exit_code"] == 0 and evidence["grade"]["raw_score"]["pass"] is False
        assert box.freeze() == first
        evidence["checks"].append("grade_reuses_freeze_and_scores_buggy_baseline")
        evidence["passed"] = True
    except Exception as exc:
        evidence["error"] = repr(exc)
        evidence["traceback"] = traceback.format_exc()
    finally:
        try:
            box.close()
            evidence["checks"].append("owned_containers_removed")
        except Exception as exc:
            evidence["passed"] = False
            evidence["cleanup_error"] = repr(exc)
        evidence["finished_at"] = _utc()
        dump_path = box.logs / "freeze_check.json"
        dump_path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        evidence["evidence"] = str(dump_path)
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--freeze-check", action="store_true")
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    args = parser.parse_args()
    directory = Path(__file__).resolve().parent
    result = freeze_check(directory, args.image) if args.freeze_check else preflight(directory, args.image)
    print(json.dumps({"passed": result["passed"], "checks": len(result["checks"]),
                      "evidence": result.get("evidence", str(directory / "sandbox_preflight.json")),
                      "run_dir": result["run_dir"], "error": result.get("error")}, indent=2))
    sys.exit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
