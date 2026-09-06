"""Real Docker D/T lifecycle with scripted actions and zero provider requests."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading

from .. import cli, environment, runner
from ..runtime_integrity import atomic_json


def action(name, **arguments):
    return {"name": name, "arguments": arguments}


class ScriptedNoProvider:
    def __init__(self, folder):
        self.folder = Path(folder)

    def complete(self, model, messages, *, role, run_id, task_id, call_id, tools, **kwargs):
        if role == "cm":
            raise AssertionError("Small lifecycle fixture unexpectedly requested compression")
        actions = [action("bash", command="python -c 'print(12345)'")]
        if role == "lead":
            for worker_id in re.findall(r'"worker_id":\s*"(worker-[0-9]+)"', messages[0]["content"]):
                if any(a["arguments"].get("worker_id") == worker_id for a in actions):
                    continue
                actions.extend([action("collect", worker_id=worker_id, wait_seconds=30),
                                action("review_worker", worker_id=worker_id, decision="reject",
                                       reason="Lifecycle fixture creates no experimental patch")])
            # The fixed roster is also available directly in the public run entry.
            entry = json.loads((self.folder / "lifecycle_entry.json").read_text(encoding="utf-8"))
            for spec in entry["worker_specs"]:
                worker_id = spec["worker_id"]
                if not any(a["arguments"].get("worker_id") == worker_id for a in actions):
                    actions.extend([action("collect", worker_id=worker_id, wait_seconds=30),
                                    action("review_worker", worker_id=worker_id, decision="reject",
                                           reason="Lifecycle fixture creates no experimental patch")])
        actions.append(action("finish", status="completed", summary="Scripted no-provider lifecycle probe"))
        calls = [{"id": f"{call_id}-{i}", **a} for i, a in enumerate(actions)]
        assistant = {"role": "assistant", "content": None, "tool_calls": [
            {"id": a["id"], "type": "function", "function": {"name": a["name"],
                "arguments": json.dumps(a["arguments"])}} for a in calls]}
        return {"call_id": call_id, "model_requested": model, "role": role, "run_id": run_id,
                "task_id": task_id, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                "cached_input_tokens": 0, "reasoning_tokens": 0, "wall_seconds": 0.,
                "error": None, "protocol_error": None, "assistant_message": assistant,
                "action": {"kind": "tools", "calls": calls}, "transport_attempt_count": 0,
                "stop_reason": "scripted_fixture_no_provider"}


def run(source_batch, destination):
    source_batch, destination = Path(source_batch).resolve(), Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    manifest = cli.load_manifest(source_batch)
    rows = []
    for arm in ("D", "T"):
        entry = dict(next(e for e in manifest["schedule"] if e["arm"] == arm))
        entry.update(run_id="lifecycle-" + arm, episode_id="lifecycle-" + arm)
        # This fixture verifies lifecycle only; no provider exists for compression.
        entry["limits_override"] = {**entry["limits_override"], "cm_enabled": False}
        runner.RESOURCE_FAILURE = threading.Event()
        run = runner.ValueRun(destination, entry, transport_factory=ScriptedNoProvider,
                              environment_factory=environment.ValueEnvironment, grade_enabled=False)
        atomic_json(run.folder / "lifecycle_entry.json", entry)
        result = run.run()
        cleanup = cli.cleanup_owned_episode(run.folder)
        passed = (result.get("infrastructure_error") is None and result.get("cleanup_confirmed") is True
                  and result.get("quiesced") is True and cleanup["confirmed"]
                  and len(result["workers"]) == len(entry["worker_specs"])
                  and all(w["status"] == "completed" for w in result["workers"]))
        evidence = {"arm": arm, "passed": passed, "real_provider_calls": 0, "official_grading": False, "fixture_cm_enabled": False,
                    "worker_count": len(result["workers"]), "cleanup": cleanup,
                    "execution_health": result["execution_health"], "result_path": str(run.folder / "result.json")}
        rows.append(evidence)
        atomic_json(destination / "smoke_result.json", {"passed": all(r["passed"] for r in rows),
                    "completed_arms": len(rows), "real_provider_calls": 0, "arms": rows,
                    "recorded_at": datetime.now(timezone.utc).isoformat()})
        print(json.dumps(evidence), flush=True)
        if not passed:
            raise RuntimeError("Real-container lifecycle fixture failed")
    return rows
