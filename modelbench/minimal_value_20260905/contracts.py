"""Frozen, finite strategy contracts; no provider or container side effects."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import random

PROTOCOL = "minimum_value_20260905_v1"
SEED = 20260905
ARMS = ("S", "L", "R2", "D", "T")
A1_TASKS = ("astropy__astropy-14995", "pydata__xarray-7229")
PUBLIC_FIELDS = ("instance_id", "repo", "base_commit", "problem_statement", "version")
FORBIDDEN = {"patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS", "hints_text", "difficulty", "eval_script"}
COMMON_OVERRIDES = {
    "max_calls": 28, "token_limit": 600_000, "wall_seconds": 1800,
    "worker_calls": 8, "lead_reserve_calls": 2, "cm_call_allowance": 12,
    "cm_enabled": True, "cm_model": "deepseek-v4-flash", "cm_provider": "deepseek",
    "cm_context_budget": 12000, "cm_keep_recent": 4, "cm_max_tokens": 4096,
    "cm_thinking": "disabled", "cm_edit_curfew": False,
    "closing_call_reserve_exempt": True, "edit_status_banner": False,
    "cm_team_memory": False, "cm_scout_distill": False, "cm_bootstrap_package": False,
}
STAGES = {
    "a1": {"task_count": 2, "episode_limit": 10, "dispatch_seconds": 9 * 3600,
           "token_admission_sum": 6_000_000, "cost_warning_usd": 40.0, "cost_stop_usd": 60.0},
    "a2": {"task_count": 16, "episode_limit": 80, "dispatch_seconds": 61 * 3600,
           "token_admission_sum": 48_000_000, "cost_warning_usd": 320.0, "cost_stop_usd": 480.0},
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def worker_specs(arm):
    if arm not in ARMS:
        raise ValueError("Unknown arm")
    if arm == "D":
        return [{"worker_id": "worker-1", "role": "test", "model": "glm-5.3"}]
    if arm == "T":
        return [{"worker_id": "worker-1", "role": "implementation", "model": "gpt-5.6-terra"},
                {"worker_id": "worker-2", "role": "test", "model": "glm-5.3"}]
    return []


def arm_entry(arm, instance, *, run_id, public_checks=None, image=None, grader_contract=None):
    if arm not in ARMS:
        raise ValueError("Unknown arm")
    if FORBIDDEN.intersection(instance) or set(instance) != set(PUBLIC_FIELDS):
        raise ValueError("Candidate instance must contain exactly the public fields")
    specs = worker_specs(arm)
    value = {
        "run_id": run_id, "episode_id": run_id, "protocol": PROTOCOL,
        "arm": arm, "strategy_id": arm, "instance": deepcopy(instance),
        "condition": "value_team" if specs else "solo",
        "lead_model": "gpt-5.6-luna" if arm == "L" else "gpt-5.6-sol",
        "worker_specs": specs, "expected_workers": len(specs),
        "worker_models": [spec["model"] for spec in specs],
        "limits_override": deepcopy(COMMON_OVERRIDES),
        "public_checks": deepcopy(public_checks or {}),
        "activation_source": "experiment_protocol",
    }
    if arm == "R2":
        value["condition"] = "r2"
        value["expected_candidates"] = 2
        value["r2_allocation"] = {
            "candidate_1": {"tokens": 270000, "work_calls": 12, "cm_calls": 6},
            "candidate_2": {"tokens": 270000, "work_calls": 12, "cm_calls": 6},
            "selector": {"tokens": 60000, "work_calls": 4, "cm_calls": 0},
        }
    if image is not None:
        value["image"] = image
    if grader_contract is not None:
        value["grader_contract"] = deepcopy(grader_contract)
    value["configuration_sha256"] = digest(value)
    return value


def schedule(stage, public, *, checks=None, images=None, grader_contract=None):
    if stage not in STAGES or len(public) != STAGES[stage]["task_count"]:
        raise ValueError("Wrong stage or task count")
    ids = [row["instance_id"] for row in public]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate tasks")
    if stage == "a1" and set(ids) != set(A1_TASKS):
        raise ValueError("A1 task identities changed")
    if stage == "a2":
        from collections import Counter
        counts = Counter(row["repo"] for row in public)
        if len(counts) < 6 or max(counts.values()) > 3:
            raise ValueError("A2 repository balance violated")
    # Randomize base order, then rotate: positions differ by at most one per arm.
    rng = random.Random(SEED + (0 if stage == "a1" else 1))
    ordered = list(public)
    rng.shuffle(ordered)
    base = list(ARMS)
    rng.shuffle(base)
    result = []
    for block, row in enumerate(ordered):
        offset = block % len(base)
        order = base[offset:] + base[:offset]
        for position, arm in enumerate(order):
            entry = arm_entry(arm, row, run_id=f"{stage}-{block + 1:02d}-{arm}",
                              public_checks=(checks or {}).get(row["instance_id"], {}),
                              image=(images or {}).get(row["instance_id"]),
                              grader_contract=grader_contract)
            entry.update(block_id=f"{stage}-{block + 1:02d}", position=position + 1,
                         stage=stage, rep=1)
            result.append(entry)
    return result


def validate_schedule(entries, stage):
    from collections import Counter, defaultdict
    if len(entries) != STAGES[stage]["episode_limit"]:
        raise ValueError("Stage episode count changed")
    groups = defaultdict(list)
    for entry in entries:
        expected = arm_entry(entry["arm"], entry["instance"], run_id=entry["run_id"],
                             public_checks=entry.get("public_checks"), image=entry.get("image"),
                             grader_contract=entry.get("grader_contract"))
        if any(entry.get(k) != v for k, v in expected.items()):
            raise ValueError("Strategy contract drift: " + entry["run_id"])
        groups[entry["instance"]["instance_id"]].append(entry["arm"])
    if any(Counter(arms) != Counter(ARMS) for arms in groups.values()):
        raise ValueError("A task block lacks a complete five-arm comparison")
    if len({entry["run_id"] for entry in entries}) != len(entries):
        raise ValueError("Duplicate run identity")
    return True
