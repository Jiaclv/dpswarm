"""Separate four-run exploratory native-tool follow-up; never changes main scores."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import importlib
import json
from pathlib import Path
import shutil
import sys

MAIN_ROOT = Path(__file__).resolve().parent
DEFAULT_ROOT = MAIN_ROOT / "native_followup"
EXPERIMENT_KIND = "native_tools_followup"
EXPERIMENT_LABEL = "GLM 原生工具调用探索性复测"
NATIVE_MODELS = ("glm-5.3", "glm-5.3-flash")
TASKS = ("SPEC5_config_system", "INT1_pipeline_repair")
COMMIT = "d185aef1916fd86a9ba554d581fd256319a973af"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_hashes(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Frozen instance contains a symlink: " + str(path))
        if path.is_file():
            result[path.relative_to(root).as_posix()] = sha(path)
    return result


def read_main_manifest(main_root: Path = MAIN_ROOT) -> dict:
    manifest = json.loads((main_root / "manifest.json").read_text(encoding="utf-8"))
    if manifest["source_commit"] != COMMIT:
        raise ValueError("Main experiment commit differs from the frozen follow-up task source")
    for name, expected in manifest["program_sha256"].items():
        if sha(main_root / name) != expected:
            raise ValueError("Frozen main program drift: " + name)
    for name, expected in manifest["control_core_sha256"].items():
        if sha(main_root.parents[1] / name) != expected:
            raise ValueError("Frozen control core drift: " + name)
    for task in TASKS:
        if tree_hashes(main_root / "instances" / task) != manifest["instances"][task]:
            raise ValueError("Frozen main instance drift: " + task)
    return manifest


def schedule(main_manifest: dict) -> list[dict]:
    rows = []
    for entry in main_manifest["schedule"]:
        if entry["condition"] == "team" and entry["executor"] in NATIVE_MODELS and entry["task_id"] in TASKS:
            rows.append({"run_id": entry["task_id"] + "__native_team__" + entry["executor"],
                "task_id": entry["task_id"], "condition": "team_native_glm", "executor": entry["executor"],
                "experiment_kind": EXPERIMENT_KIND, "experiment_label": EXPERIMENT_LABEL})
    if len(rows) != 4 or len({(r["task_id"], r["executor"]) for r in rows}) != 4:
        raise ValueError("Follow-up requires exactly the two GLMs crossed with the two frozen tasks")
    return rows


def build_manifest(root: Path, main_root: Path, main: dict) -> dict:
    if str(main_root) not in sys.path:
        sys.path.insert(0, str(main_root))
    from native_tools import ADAPTER_MODE, GPT_MODE, OFFICIAL_SOURCES
    sources = ("native_followup.py", "native_tools.py", "test_native_tools.py", "native_followup/PLAN.md")
    return {"experiment_kind": EXPERIMENT_KIND, "experiment_label": EXPERIMENT_LABEL,
        "preregistered_main_score": False,
        "design_status": "exploratory sensitivity experiment after observed text-protocol failures",
        "source_commit": main["source_commit"], "seed": main["seed"], "schedule": schedule(main),
        "limits": main["limits"], "run_token_boundary": main["run_token_boundary"], "parallel_runs": 2,
        "planner": "gpt-5.6-sol", "verifier": "gpt-5.6-terra", "executor_models": list(NATIVE_MODELS),
        "image_id": main["image_id"], "image_repo_digests": main.get("image_repo_digests"),
        "main_manifest_sha256": sha(main_root / "manifest.json"),
        "main_program_sha256": main["program_sha256"], "control_core_sha256": main["control_core_sha256"],
        "program_sha256": {name: sha(main_root / name) for name in sources},
        "instances": {task: tree_hashes(root / "instances" / task) for task in TASKS},
        "main_instances": {task: main["instances"][task] for task in TASKS},
        "conditions": {"glm_adapter_mode": ADAPTER_MODE, "gpt_adapter_mode": GPT_MODE,
            "glm": {"tools": "official native function declarations", "tool_choice": "auto",
                "thinking": {"type": "enabled"}, "reasoning_effort": "max", "temperature": 1.0,
                "max_tokens": 32768, "reasoning_history": "preserve exact prior assistant.reasoning_content",
                "tool_history": "preserve native IDs and pair role:tool results", "model_retry": False},
            "gpt": {"model_reasoning_effort": "max", "service_tier": "fast", "transport": "unchanged frozen Codex CLI"},
            "normalization": "native tool calls to frozen action JSON; native text-only response to final",
            "inputs": "fresh copies of original frozen instances and fresh role messages; no main-run output or grader feedback",
            "role_information": "Planner/Verifier full spec; Executor same original brief and internal plan",
            "tool_execution": "unchanged frozen Docker sandbox and AgentLoop; batch limit eight",
            "start_gate": "all 12 main matrix result.json files must exist before follow-up run"},
        "official_documentation": OFFICIAL_SOURCES}


def prepare(root: Path = DEFAULT_ROOT, main_root: Path = MAIN_ROOT) -> dict:
    root, main_root = root.resolve(), main_root.resolve()
    if root == main_root or root.is_relative_to(main_root / "results"):
        raise ValueError("Follow-up output must be separate from the main experiment and its results")
    main = read_main_manifest(main_root)
    root.mkdir(parents=True, exist_ok=True)
    for task in TASKS:
        destination = root / "instances" / task
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(main_root / "instances" / task, destination)
        if tree_hashes(destination) != main["instances"][task]:
            raise ValueError("Follow-up instance is not an untouched copy of the main frozen instance: " + task)
    manifest = build_manifest(root, main_root, main)
    path = root / "manifest.json"
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        if {key: value for key, value in old.items() if key != "created_at"} != manifest:
            raise ValueError("Frozen native follow-up manifest drift")
        return old
    manifest["created_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def assert_main_completed(main_root: Path = MAIN_ROOT) -> None:
    main = read_main_manifest(main_root)
    missing = [entry["run_id"] for entry in main["schedule"]
               if not (main_root / "results" / entry["run_id"] / "result.json").is_file()]
    if missing:
        raise RuntimeError("Main matrix has not completed; native follow-up must wait: " + ", ".join(missing))


def configured_runner(root: Path):
    """Call only in this independent CLI process, never in a main-run process."""
    if str(MAIN_ROOT) not in sys.path:
        sys.path.insert(0, str(MAIN_ROOT))
    runner = importlib.import_module("run_experiment")
    from native_tools import HybridTransport
    runner.ROOT = root.resolve()
    runner.ExperimentTransport = HybridTransport
    return runner


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run"))
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    parser.add_argument("--run-id")
    args = parser.parse_args()
    manifest = prepare(args.root)
    if args.command == "prepare":
        print(json.dumps({"prepared": True, "experiment_kind": EXPERIMENT_KIND, "runs": len(manifest["schedule"])}))
        return
    assert_main_completed()
    runner = configured_runner(args.root)
    if runner.LIMITS != manifest["limits"] or runner.TOKEN_BOUNDARY != manifest["run_token_boundary"]:
        raise ValueError("Frozen runner budget differs from native follow-up manifest")
    entries = [entry for entry in manifest["schedule"] if not args.run_id or entry["run_id"] == args.run_id]
    if not entries:
        raise ValueError("No matching frozen native follow-up run_id")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(runner.TeamRun(entry).run): entry for entry in entries}
        for future in as_completed(futures):
            result = future.result()
            print("NATIVE_RUN_FINISHED " + json.dumps({key: result.get(key) for key in
                ("run_id", "status", "score", "total_tokens", "wall_seconds", "experiment_kind")}), flush=True)


if __name__ == "__main__":
    main()
