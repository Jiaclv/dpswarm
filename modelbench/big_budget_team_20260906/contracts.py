"""Finite entries derived from the saved ten-condition, 180-cell plan."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import csv
import hashlib
import io
import json
from pathlib import Path
import re

from modelbench.minimal_value_20260905.contracts import PUBLIC_FIELDS, FORBIDDEN, digest
from modelbench.minimal_value_20260905.runtime_integrity import resolve_limits

HERE = Path(__file__).resolve().parent
PROTOCOL = "big_budget_team_20260906_v1"
PLAN_PROTOCOL = "big_budget_team_20260906_plan_v1"
CONDITIONS = ("D00", "D11", "T00", "T10", "T01", "T11", "F-S", "F-T", "S-FF", "S-S")
_LAYOUTS = {
    "D00": ("D", "gpt-5.6-sol", [("test", "glm-5.3")], 0, 0),
    "D11": ("D", "gpt-5.6-sol", [("test", "glm-5.3")], 1, 1),
    "T00": ("T", "gpt-5.6-sol", [("implementation", "gpt-5.6-terra"), ("test", "glm-5.3")], 0, 0),
    "T10": ("T", "gpt-5.6-sol", [("implementation", "gpt-5.6-terra"), ("test", "glm-5.3")], 1, 0),
    "T01": ("T", "gpt-5.6-sol", [("implementation", "gpt-5.6-terra"), ("test", "glm-5.3")], 0, 1),
    "T11": ("T", "gpt-5.6-sol", [("implementation", "gpt-5.6-terra"), ("test", "glm-5.3")], 1, 1),
    "F-S": ("S", "glm-5.3-flash", [], 1, 1),
    "F-T": ("T", "glm-5.3-flash", [("implementation", "glm-5.3-flash"), ("test", "glm-5.3-flash")], 1, 1),
    "S-FF": ("T", "gpt-5.6-sol", [("implementation", "glm-5.3-flash"), ("test", "glm-5.3-flash")], 1, 1),
    "S-S": ("S", "gpt-5.6-sol", [], 1, 1),
}
_MECHANISM = {
    "cm_enabled": True, "cm_model": "deepseek-v4-flash", "cm_provider": "deepseek",
    "cm_context_budget": 12000, "cm_keep_recent": 4, "cm_max_tokens": 4096,
    "cm_reservation_slack": 8192, "cm_thinking": "disabled", "cm_edit_curfew": False,
    "cm_team_memory": False, "cm_scout_distill": False, "cm_bootstrap_package": False,
    "edit_status_banner": False, "closing_call_reserve_exempt": True,
}
_NUMERIC_COLUMNS = {
    "priority", "phase", "rep", "position", "token_limit", "max_calls",
    "worker_calls", "cm_call_allowance", "wall_seconds", "candidate_container_slots",
}
_CELL_COLUMNS = _NUMERIC_COLUMNS | {"run_id", "block_id", "instance_id", "condition_id", "status"}


def _check_plan(plan):
    if not isinstance(plan, dict) or plan.get("protocol") != PLAN_PROTOCOL:
        raise ValueError("Wrong saved experiment plan protocol")
    scope = plan.get("scope", {})
    if any(scope.get(k) != v for k, v in {"tasks": 6, "conditions": 10, "repetitions": 3,
                                         "generation_attempts": 180, "generation_retries": 0}.items()):
        raise ValueError("Saved experiment scope changed")
    tasks = plan.get("task_ids", [])
    if len(tasks) != 6 or len(set(tasks)) != 6:
        raise ValueError("Saved task identities are not unique")
    arms = plan.get("arms", [])
    if [arm.get("condition_id") for arm in arms] != list(CONDITIONS):
        raise ValueError("Saved condition identities or ordering changed")
    for arm in arms:
        base, lead, roles, token_factor, call_factor = _LAYOUTS[arm["condition_id"]]
        expected_limits = {"token_limit": 2400000 if token_factor else 600000,
                           "max_calls": 160 if call_factor else 28,
                           "worker_calls": 48 if call_factor else 8,
                           "cm_call_allowance": 64 if call_factor else 12,
                           "lead_reserve_calls": 2, "wall_seconds": 7200}
        if (arm.get("base_arm") != base or arm.get("lead_model") != lead
                or arm.get("worker_specs") != [{"role": r, "model": m} for r, m in roles]
                or arm.get("token_factor") != token_factor or arm.get("call_bundle_factor") != call_factor
                or arm.get("limits_override") != expected_limits
                or arm.get("container_slots") != 1 + len(roles)):
            raise ValueError("Saved condition layout or budget drift: " + arm["condition_id"])
    mechanism = plan.get("shared_mechanism", {})
    if any(type(mechanism.get(k)) is not type(v) or mechanism[k] != v for k, v in _MECHANISM.items()):
        raise ValueError("Saved shared mechanism changed")
    if mechanism.get("ordinary_call_max_tokens") != 32768 or mechanism.get("lead_resume_after_budget_rejection") is not False:
        raise ValueError("Saved admission or termination mechanism changed")
    resources = plan.get("planned_resources", {})
    if resources.get("candidate_memory_gib") != 1 or resources.get("candidate_cpu_limit") != 2:
        raise ValueError("Saved candidate resource limits changed")
    return plan


def load_plan(path=None):
    """Read the saved plan; source freezing remains the controller's responsibility."""
    return _check_plan(json.loads(Path(path or HERE / "plan_contract.json").read_text(encoding="utf-8-sig")))


def _cell(value):
    if not isinstance(value, dict) or set(value) != _CELL_COLUMNS:
        raise ValueError("Planned cell must contain exactly the saved CSV columns")
    result = deepcopy(value)
    for key in _NUMERIC_COLUMNS:
        raw = result[key]
        if type(raw) is int:
            continue
        if not isinstance(raw, str) or not re.fullmatch(r"[0-9]+", raw):
            raise ValueError("Invalid cell integer: " + key)
        result[key] = int(raw)
    return result


def load_cells(path=None, *, plan=None):
    plan = load_plan() if plan is None else _check_plan(plan)
    raw = Path(path or HERE / "planned_cells.csv").read_bytes()
    expected_sha = plan.get("artifacts", {}).get("planned_cells.csv", {}).get("sha256")
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError("Planned cell source hash changed")
    rows = [_cell(row) for row in csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))]
    if len(rows) != 180 or [row["priority"] for row in rows] != list(range(1, 181)):
        raise ValueError("Planned cell count or priority changed")
    groups, identities = {}, set()
    catalog = {arm["condition_id"]: arm for arm in plan["arms"]}
    for row in rows:
        cid, task, rep = row["condition_id"], row["instance_id"], row["rep"]
        if cid not in catalog or task not in plan["task_ids"] or rep not in (1, 2, 3):
            raise ValueError("Unplanned condition, task or repetition")
        arm = catalog[cid]
        block = f"b1-r{rep}-q{plan['task_ids'].index(task) + 1:02d}"
        if (row["block_id"] != block or row["run_id"] != f"{block}-{cid}"
                or row["phase"] != (1 if rep == 1 else 2)
                or row["candidate_container_slots"] != arm["container_slots"]
                or row["status"] != "planned_not_launched"
                or any(row[k] != arm["limits_override"][k] for k in
                       ("token_limit", "max_calls", "worker_calls", "cm_call_allowance", "wall_seconds"))):
            raise ValueError("Planned cell disagrees with its condition: " + row["run_id"])
        ident = (task, cid, rep)
        if ident in identities:
            raise ValueError("Duplicate planned experiment cell")
        identities.add(ident)
        groups.setdefault(block, []).append(row)
    if len(groups) != 18 or any(Counter(row["condition_id"] for row in rows) != Counter(CONDITIONS)
                               or sorted(row["position"] for row in rows) != list(range(1, 11))
                               for rows in groups.values()):
        raise ValueError("Incomplete ten-condition planned block")
    return rows


def _public_inputs(instance, checks, image, grader_contract):
    if not isinstance(instance, dict) or set(instance) != set(PUBLIC_FIELDS):
        raise ValueError("Candidate instance must contain exactly the public fields")
    if any(not isinstance(value, str) or not value.strip() for value in instance.values()):
        raise ValueError("Candidate public fields must be nonempty strings")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", instance["base_commit"]):
        raise ValueError("Candidate base commit must be a full commit hash")
    if not isinstance(checks, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                          for k, v in checks.items()) or FORBIDDEN.intersection(checks):
        raise ValueError("Public checks must be a flat command mapping without hidden fields")
    if image is not None and (not isinstance(image, str) or not image.strip()):
        raise ValueError("Image identity must be a nonempty string")
    if grader_contract is not None and not isinstance(grader_contract, dict):
        raise ValueError("Grader contract must be a mapping")


def _entry(cell, instance, checks, image, grader_contract, plan):
    _public_inputs(instance, checks, image, grader_contract)
    if instance["instance_id"] != cell["instance_id"]:
        raise ValueError("Public task does not match planned cell")
    arm = next(a for a in plan["arms"] if a["condition_id"] == cell["condition_id"])
    specs = [{"worker_id": f"worker-{i + 1}", **spec} for i, spec in enumerate(arm["worker_specs"])]
    # Only defaults are reused; the old strategy validator cannot accept Flash routes.
    from modelbench.minimal_value_20260905.runner import LIMITS
    overrides = {**_MECHANISM, **arm["limits_override"], "memory": "1g", "cpus": 2}
    effective = resolve_limits({**LIMITS, "active_workers": len(specs), "delegations": len(specs)}, overrides)
    value = {
        "protocol": PROTOCOL, "plan_sha256": digest(plan),
        "run_id": cell["run_id"], "episode_id": cell["run_id"],
        "condition_id": arm["condition_id"], "strategy_id": arm["condition_id"],
        "base_arm": arm["base_arm"], "arm": arm["base_arm"],
        "condition": "value_team" if specs else "solo",
        "lead_model": arm["lead_model"], "worker_specs": specs,
        "worker_models": [spec["model"] for spec in specs], "worker_model": None,
        "expected_workers": len(specs), "token_factor": arm["token_factor"],
        "call_bundle_factor": arm["call_bundle_factor"],
        "limits_override": overrides, "effective_limits": effective,
        "instance": deepcopy(instance), "public_checks": deepcopy(checks),
        "activation_source": "experiment_protocol",
        **{key: cell[key] for key in ("block_id", "phase", "rep", "position", "priority", "candidate_container_slots")},
    }
    if image is not None:
        value["image"] = image
    if grader_contract is not None:
        value["grader_contract"] = deepcopy(grader_contract)
    value["configuration_sha256"] = digest(value)
    return value


def build_entry(cell, public_instance, public_checks=None, image=None, grader_contract=None, *, plan=None):
    plan = load_plan() if plan is None else _check_plan(plan)
    normalized = _cell(cell)
    matching = [row for row in load_cells(plan=plan) if row["run_id"] == normalized["run_id"]]
    if matching != [normalized]:
        raise ValueError("Cell is not an unchanged member of the saved plan")
    return _entry(normalized, public_instance, {} if public_checks is None else public_checks,
                  image, grader_contract, plan)


def validate_entry(entry, *, plan=None):
    plan = load_plan() if plan is None else _check_plan(plan)
    if not isinstance(entry, dict):
        raise ValueError("Entry must be a mapping")
    matching = [row for row in load_cells(plan=plan) if row["run_id"] == entry.get("run_id")]
    if len(matching) != 1:
        raise ValueError("Entry is not in the saved plan")
    expected = _entry(matching[0], entry.get("instance"), entry.get("public_checks"),
                      entry.get("image"), entry.get("grader_contract"), plan)
    if entry != expected or digest(entry) != digest(expected):
        raise ValueError("Entry identity, models, budget or configuration hash drift")
    return deepcopy(expected)


def validate_schedule(entries, *, plan=None):
    plan = load_plan() if plan is None else _check_plan(plan)
    expected = load_cells(plan=plan)
    if not isinstance(entries, list) or len(entries) != len(expected):
        raise ValueError("Execution schedule must contain all 180 new cells")
    if [entry.get("run_id") for entry in entries] != [cell["run_id"] for cell in expected]:
        raise ValueError("Execution schedule identities or frozen ordering changed")
    tasks = {}
    for entry in entries:
        validate_entry(entry, plan=plan)
        key = entry["instance"]["instance_id"]
        public = {name: entry.get(name) for name in ("instance", "public_checks", "image", "grader_contract")}
        if key in tasks and tasks[key] != public:
            raise ValueError("A task has unequal public inputs across conditions")
        tasks[key] = public
    return True
