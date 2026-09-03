"""Frozen, auditable TeamBench role experiment. Candidate tools run only in Docker."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import random
import shutil
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / "TeamBench"
sys.path.insert(0, str(VENDOR))
from generators.registry import get_generator
from harness.agent_interface import (
    make_planner_config, make_executor_config, make_verifier_config,
    tools_to_standard_declarations,
)
from sandbox import RoleSandbox, DEFAULT_IMAGE
from transports import ExperimentTransport, MODELS, TEXT_TOOL_PROTOCOL

COMMIT = "d185aef1916fd86a9ba554d581fd256319a973af"
TASKS = ("SPEC5_config_system", "INT1_pipeline_repair")
LIMITS = {"planner": 2, "executor": 6, "verifier": 4, "repair": 3, "reverify": 3, "solo": 18}
TOKEN_BOUNDARY = 600_000


def utc():
    return datetime.now(timezone.utc).isoformat()


def dump(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append(path: Path, value):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(value, ensure_ascii=False) + "\n")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree_hashes(root):
    return {str(p.relative_to(root)).replace("\\", "/"): sha(p)
            for p in sorted(Path(root).rglob("*")) if p.is_file() and not p.is_symlink()}


def git(*args, binary=False):
    r = subprocess.run(["git", "-C", str(VENDOR), *args], check=True, capture_output=True)
    return r.stdout if binary else r.stdout.decode("utf-8").strip()


def materialize(task_id: str, destination: Path):
    """Fresh instance from committed blob bytes; ground truth never in role reports."""
    destination.mkdir(parents=True, exist_ok=False)
    task_dir, workspace = destination / "task", destination / "workspace"
    task_dir.mkdir()
    workspace.mkdir()
    generated = get_generator(task_id).generate(0) if task_id.startswith("SPEC5") else None
    prefix = "tasks/" + task_id + "/"
    for name in git("ls-tree", "-r", "--name-only", COMMIT, prefix).splitlines():
        relative = name[len(prefix):]
        if relative.startswith("workspace/"):
            if generated:
                continue
            output = workspace / relative[len("workspace/"):]
        else:
            output = task_dir / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(git("show", COMMIT + ":" + name, binary=True))
    grader = destination / "grader"
    grader.mkdir()
    if generated:
        (task_dir / "spec.md").write_text(generated.spec_md, encoding="utf-8")
        (task_dir / "brief.md").write_text(generated.brief_md, encoding="utf-8")
        for relative, content in generated.workspace_files.items():
            output = (workspace / relative).resolve()
            if not output.is_relative_to(workspace.resolve()):
                raise ValueError("Generator path escaped workspace")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        if generated.corpus_files:
            raise ValueError("This frozen pilot has no corpus tasks")
        dump(grader / "expected.json", generated.expected)
    metadata = {"task_id": task_id, "source_commit": COMMIT,
                "instance": "generated", "seed": 0} if generated else {
                "task_id": task_id, "source_commit": COMMIT, "instance": "tracked-static", "seed": None}
    metadata["files"] = tree_hashes(destination)
    dump(destination / "instance.json", metadata)


def schedule():
    rows = []
    for task_id in TASKS:
        for model in MODELS:
            rows.append({"run_id": task_id + "__team__" + model, "task_id": task_id,
                         "condition": "team", "executor": model})
        rows.append({"run_id": task_id + "__solo__gpt-5.6-sol", "task_id": task_id,
                     "condition": "solo", "executor": "gpt-5.6-sol"})
    random.Random(20260903).shuffle(rows)
    return rows


def prepare():
    if git("rev-parse", "HEAD") != COMMIT:
        raise RuntimeError("TeamBench commit drift")
    if git("status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("Tracked TeamBench source has local modifications")
    for task in TASKS:
        target = ROOT / "instances" / task
        if not target.exists():
            materialize(task, target)
    image = subprocess.run(["docker", "image", "inspect", DEFAULT_IMAGE], capture_output=True,
                           text=True, encoding="utf-8", check=True)
    inspect = json.loads(image.stdout)[0]
    files = ["PLAN.md", "run_experiment.py", "sandbox.py", "transports.py", "control_bridge.py", "Dockerfile"]
    manifest = {"created_at": utc(), "source_commit": COMMIT, "seed": 20260903,
                "schedule": schedule(), "limits": LIMITS, "run_token_boundary": TOKEN_BOUNDARY,
                "parallel_runs": 2, "python": sys.version, "platform": platform.platform(),
                "image_id": inspect["Id"], "image_repo_digests": inspect.get("RepoDigests"),
                "program_sha256": {name: sha(ROOT / name) for name in files},
                "control_core_sha256": {str(p.relative_to(ROOT.parents[1])).replace("\\", "/"): sha(p)
                    for p in sorted((ROOT.parents[1] / "dpswarm-plugin" / "dpswarm").glob("*.py"))},
                "instances": {task: tree_hashes(ROOT / "instances" / task) for task in TASKS}}
    target = ROOT / "manifest.json"
    if target.exists():
        old = json.loads(target.read_text(encoding="utf-8"))
        for key in ("source_commit", "schedule", "limits", "program_sha256", "control_core_sha256", "instances", "image_id"):
            if manifest[key] != old[key]:
                raise RuntimeError("Frozen manifest drift: " + key)
    else:
        dump(target, manifest)
    return manifest


def parse_action(text):
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Response must be one JSON object")
    if value.get("type") == "final" and isinstance(value.get("content"), str):
        return value
    if value.get("type") != "tool_calls":
        raise ValueError("type must be final or tool_calls")
    calls = value.get("calls")
    if not isinstance(calls, list) or not 1 <= len(calls) <= 8:
        raise ValueError("Request between 1 and 8 tool calls")
    if any(not isinstance(c, dict) or not isinstance(c.get("name"), str)
           or not isinstance(c.get("arguments"), dict) for c in calls):
        raise ValueError("Each tool call requires name and arguments")
    return value


class TeamRun:
    def __init__(self, entry):
        self.entry = entry
        self.folder = ROOT / "results" / entry["run_id"]
        self.calls = []
        self.phases = []
        self.histories = {}
        self.seen = {}
        self.dialogue = []
        self.model_map = {"planner": "gpt-5.6-sol", "executor": entry["executor"],
                          "verifier": "gpt-5.6-terra", "oracle": "gpt-5.6-sol"}

    def messages_for(self, role):
        new = [m for m in self.dialogue[self.seen.get(role, 0):] if m["to"] in (role, "all")]
        self.seen[role] = len(self.dialogue)
        return new

    def config(self, role):
        task = str(self.folder / "task")
        msg = str(self.folder / "messages")
        paths = dict(workspace_dir=str(self.folder / "workspace"), reports_dir=str(self.folder / "reports"),
                     messages_dir=msg, submission_dir=str(self.folder / "submission"), task_dir=task)
        if role == "planner":
            config = make_planner_config(task + "/spec.md", msg, task)
        elif role == "executor":
            config = make_executor_config(task + "/brief.md", **paths)
        else:
            config = make_verifier_config(task + "/spec.md", **paths)
        if role == "oracle":
            config.system_prompt = (
                "You are a solo agent with the full specification. Implement the required changes, "
                "run tests and independently check all requirements. You have read/write/run access "
                "to the workspace and submission. Write /shared/submission/attestation.json "
                "with verdict pass or fail and evidence before finishing.")
        declarations = tools_to_standard_declarations(config.tools)
        if role == "oracle":
            declarations = [t for t in declarations if t["name"] != "send_message"]
        extra = ("\nContainer paths: /task/spec.md (planner/verifier/solo only), /task/brief.md, "
                 "/shared/workspace, /shared/reports, /shared/submission. No internet. "
                 "The hidden grader and expected answers are unavailable. All actual actions must "
                 "be requested via the JSON tools; prose/code alone does not change files. "
                 "Batch independent actions where useful; up to 8 tools per model response. "
                 "Finish using the JSON final response only after executing your role's work.")
        if role == "verifier":
            extra += ("\nWorkspace is OS read-only. For checks that write outputs, use a FRESH copy each time: "
                      "checkdir=$(mktemp -d); cp -a /shared/workspace/. \"$checkdir/\"; cd \"$checkdir\"; "
                      "then run checks in that same command. /tmp is writable. Never reuse an earlier snapshot. "
                      "The attestation belongs to /shared/submission/attestation.json.")
        return config.system_prompt + extra, declarations

    def tool(self, role, call, allowed):
        name, args = call["name"], call["arguments"]
        if name not in allowed:
            return {"stdout": "", "stderr": "Tool not authorized for this role", "exit_code": 1}
        if name != "send_message":
            return self.box.tool(role, name, args)
        target, content = args.get("to"), args.get("content")
        if target not in ("planner", "executor", "verifier", "all") or not isinstance(content, str) or not content:
            return {"stdout": "", "stderr": "Invalid internal role message", "exit_code": 1}
        m = {"ts": utc(), "role": role, "to": target, "content": content, "type": "message"}
        self.dialogue.append(m)
        append(self.folder / "messages" / "dialogue.jsonl", m)
        return {"stdout": "Message sent to " + target, "stderr": "", "exit_code": 0}

    def phase(self, role, phase, prompt):
        phase_dir = self.folder / "phases" / phase
        phase_dir.mkdir(parents=True)
        messages = self.histories.setdefault(role, [])
        messages.append({"role": "user", "content": prompt})
        system, declarations = self.config(role)
        system += "\n\n" + TEXT_TOOL_PROTOCOL + "\nAvailable tools:\n" + json.dumps(declarations)
        allowed = {t["name"] for t in declarations}
        handle = self.control.start_role(role, phase)
        phase_calls = []
        info = {"role": role, "phase": phase, "model": self.model_map[role], "started_at": utc(),
                "status": "turn_limit", "tool_calls": 0, "protocol_errors": 0}
        start = time.monotonic()
        for turn in range(LIMITS[phase]):
            if sum(c.get("total_tokens") or 0 for c in self.calls) >= TOKEN_BOUNDARY:
                info["status"] = "token_boundary"
                break
            new = self.messages_for(role)
            if new:
                messages.append({"role": "user", "content": "New internal messages:\n" + json.dumps(new, ensure_ascii=False)})
            messages.append({"role": "user", "content": f"Phase {phase}: model call {turn+1}/{LIMITS[phase]}."})
            record = self.transport.complete(self.model_map[role], [{"role": "system", "content": system}] + messages,
                    run_id=self.entry["run_id"], role=role, task_id=self.entry["task_id"])
            self.calls.append(record)
            phase_calls.append(record["call_id"])
            self.control.record_call(handle, record)
            print(f"{self.entry['run_id']} {phase} {turn+1} tokens={record.get('total_tokens')} seconds={record['wall_seconds']:.1f}", flush=True)
            if record.get("error"):
                info["status"] = "transport_error"
                info["error"] = record["error"]
                break
            messages.append({"role": "assistant", "content": record["text"]})
            step = {"call_id": record["call_id"], "turn": turn+1}
            try:
                action = parse_action(record["text"])
                step["action"] = action
                if action["type"] == "final":
                    info["status"] = "final"
                    info["final"] = action["content"]
                    dump(phase_dir / f"turn_{turn+1:03d}.json", step)
                    break
                results = []
                for call in action["calls"]:
                    result = self.tool(role, call, allowed)
                    results.append({"call": call, "result": result})
                    info["tool_calls"] += 1
                step["tool_results"] = results
                # Full tool output is on disk; model-visible truncation follows TeamBench limits.
                visible = [{"name": r["call"]["name"], "id": r["call"].get("id"),
                            "stdout": r["result"]["stdout"][:4000], "stderr": r["result"]["stderr"][:2000],
                            "exit_code": r["result"]["exit_code"]} for r in results]
                messages.append({"role": "user", "content": "Tool results:\n" + json.dumps(visible, ensure_ascii=False)})
            except (ValueError, TypeError) as exc:
                info["protocol_errors"] += 1
                step["protocol_error"] = str(exc)
                messages.append({"role": "user", "content": "Protocol error (this call was consumed): " + str(exc) + "\n" + TEXT_TOOL_PROTOCOL})
            dump(phase_dir / f"turn_{turn+1:03d}.json", step)
        info.update(completed_at=utc(), wall_seconds=time.monotonic()-start, call_ids=phase_calls)
        dump(phase_dir / "phase.json", info)
        dump(phase_dir / "conversation.json", messages)
        self.phases.append(info)
        self.control.submit_role(handle, info)
        return info

    def attestation(self):
        path = self.folder / "submission" / "attestation.json"
        try:
            if path.is_symlink() or not path.resolve().is_relative_to((self.folder / "submission").resolve()):
                raise ValueError("Attestation path escaped submission")
            if path.stat().st_size > 1_048_576:
                raise ValueError("Attestation exceeds 1 MiB")
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {"invalid": value}
        except (OSError, ValueError) as exc:
            return {"error": str(exc)}

    def run(self):
        from control_bridge import ExperimentControl
        if self.folder.exists():
            result_file = self.folder / "result.json"
            if result_file.exists():
                return json.loads(result_file.read_text(encoding="utf-8"))
            raise RuntimeError("Unfinished run exists; explicit audit required: " + str(self.folder))
        shutil.copytree(ROOT / "instances" / self.entry["task_id"], self.folder)
        (self.folder / "messages").mkdir()
        self.transport = ExperimentTransport(ROOT)
        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        self.box = RoleSandbox(self.folder / "task", self.folder, image=manifest["image_id"])
        self.control = None
        result = {**self.entry, "started_at": utc(), "status": "running"}
        dump(self.folder / "started.json", result)
        start = time.monotonic()
        try:
            self.control = ExperimentControl(self.folder, self.model_map, self.entry["task_id"])
            self.box.start()
            spec = (self.folder / "task" / "spec.md").read_text(encoding="utf-8")
            brief = (self.folder / "task" / "brief.md").read_text(encoding="utf-8")
            if self.entry["condition"] == "solo":
                self.phase("oracle", "solo", "Complete this task and validate the result. Full specification:\n" + spec)
            else:
                self.phase("planner", "planner", "Plan this task and send the executor an actionable plan. Full specification:\n" + spec)
                self.phase("executor", "executor", "Implement this task using the planner's messages. Task brief:\n" + brief)
                self.phase("verifier", "verifier", "Independently validate the submitted workspace and write attestation.json. Full specification:\n" + spec)
                first = self.attestation()
                dump(self.folder / "first_attestation.json", first)
                if first.get("verdict") != "pass":
                    self.phase("executor", "repair", "Address verifier feedback and finish the task. Verifier attestation:\n" + json.dumps(first, ensure_ascii=False))
                    prior = self.folder / "submission" / "attestation.json"
                    if prior.is_file() and not prior.is_symlink():
                        prior.rename(self.folder / "submission" / "attestation.previous.json")
                    self.phase("verifier", "reverify", "Recheck the repaired workspace using a NEW temporary copy of its CURRENT contents. Run checks and write a NEW attestation.json.")
            self.box.freeze()
            result["attestation"] = self.attestation()
            dump(self.folder / "workspace_before_grade.sha256.json", tree_hashes(self.folder / "workspace"))
            grading = self.box.grade()
            result["grading"] = grading
            result["score"] = grading.get("raw_score")
            score = result["score"]
            valid_score = (isinstance(score, dict) and isinstance(score.get("pass"), bool)
                           and isinstance(score.get("secondary"), dict)
                           and all(isinstance(score["secondary"].get(k), (int, float))
                                   for k in ("checks_passed", "checks_total", "partial_score")))
            result["status"] = "scored" if (valid_score and grading.get("exit_code") == 0
                                and not grading.get("timed_out") and not grading.get("score_parse_error")) else "grader_error"
            if any(p["status"] == "transport_error" for p in self.phases):
                result["status"] = "transport_error"
            result["control"] = self.control.finish(grading, result)
        except Exception as exc:
            result["status"] = "infrastructure_error"
            result["error"] = {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
        finally:
            try:
                if self.control is not None:
                    self.control.close()
            except Exception as exc:
                result["control_cleanup_error"] = str(exc)
            try:
                self.box.close()
            except Exception as exc:
                result["sandbox_cleanup_error"] = str(exc)
            result.update(completed_at=utc(), wall_seconds=time.monotonic()-start, phases=self.phases,
                          call_ids=[c["call_id"] for c in self.calls], call_count=len(self.calls),
                          input_tokens=sum(c.get("input_tokens") or 0 for c in self.calls),
                          output_tokens=sum(c.get("output_tokens") or 0 for c in self.calls),
                          total_tokens=sum(c.get("total_tokens") or 0 for c in self.calls),
                          usage_complete=all(c.get("input_tokens") is not None and c.get("output_tokens") is not None for c in self.calls))
            dump(self.folder / "result.json", result)
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "run"))
    parser.add_argument("--workers", type=int, default=2, choices=(1, 2))
    parser.add_argument("--run-id")
    args = parser.parse_args()
    manifest = prepare()
    if args.command == "prepare":
        print(json.dumps({"prepared": True, "runs": len(manifest["schedule"])}))
        return
    entries = [e for e in manifest["schedule"] if not args.run_id or e["run_id"] == args.run_id]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(TeamRun(e).run): e for e in entries}
        for future in as_completed(futures):
            value = future.result()
            print("RUN_FINISHED " + json.dumps({k: value.get(k) for k in ("run_id", "status", "score", "total_tokens", "wall_seconds")}), flush=True)


if __name__ == "__main__":
    main()
