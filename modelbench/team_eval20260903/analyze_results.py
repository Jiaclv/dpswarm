"""Offline, stdlib-only snapshots of the frozen TeamBench pilot.

Never imports the runner, transports, model configuration, or control plane.
Only completed main-ledger calls, deduplicated by call_id, enter usage totals.
Unknown measurements stay null; known subtotals and coverage are separate.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import io
import json
import math
from pathlib import Path
import random
from typing import Any

ROOT = Path(__file__).resolve().parent
MODELS = ("glm-5.3", "glm-5.3-flash", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
TASKS = ("SPEC5_config_system", "INT1_pipeline_repair")
USAGE = ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "reasoning_tokens")
ROLES = ("planner", "executor", "verifier", "oracle")
PHASE_ROLES = {"planner": "planner", "executor": "executor", "repair": "executor",
               "verifier": "verifier", "reverify": "verifier", "solo": "oracle"}


def number(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def boolean(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def obj(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def read_json(path: Path, warnings: list[str]) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        warnings.append(f"暂未读取完整 JSON：{path}: {type(exc).__name__}")
        return None


def read_ledger(path: Path, warnings: list[str]) -> tuple[dict[str, dict], dict[str, dict], int]:
    completed, started = {}, {}
    duplicates = 0
    if not path.is_file():
        return completed, started, duplicates
    try:
        with path.open(encoding="utf-8-sig") as stream:
            for line_no, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except ValueError:
                    warnings.append(f"忽略正在写入或无效的 ledger 行：{path}:{line_no}")
                    continue
                if not isinstance(value, dict) or not isinstance(value.get("call_id"), str):
                    warnings.append(f"忽略缺少 call_id 的 ledger 行：{path}:{line_no}")
                    continue
                call_id = value["call_id"]
                if value.get("event") == "started":
                    started[call_id] = value
                elif value.get("event") == "completed":
                    duplicates += int(call_id in completed)
                    completed[call_id] = value  # Last completed record wins, exactly once.
    except OSError as exc:
        warnings.append(f"无法读取 ledger：{path}: {type(exc).__name__}")
    return completed, started, duplicates


def normalize_call(record: dict, matrix_ids: set[str]) -> dict:
    fields = ("call_id", "run_id", "task_id", "role", "started_at", "completed_at",
              "model_requested", "model_reported", "effort_requested", "effort_reported",
              "service_tier_requested", "service_tier_reported", "wall_seconds", "stop_reason",
              "usage_source", "total_tokens_source", "cap_enforced", "max_tokens_requested",
              "retry_attempted", "reconnect_detected", "attempt_count", "transport_attempt_count",
              "http_status", "returncode", "raw_artifacts", "adapter_mode",
              "native_tool_calls_requested")
    row = {key: record.get(key) for key in fields}
    row["scope"] = "matrix" if record.get("run_id") in matrix_ids else "unscored_other"
    row["status"] = "error" if record.get("error") else "completed"
    row["error"] = record.get("error")
    row["native_tools_used"] = number(record.get("tools_used"))
    row["external_tool_calls"] = 0 if record.get("error") else None
    row["external_tool_error_count"] = 0 if record.get("error") else None
    row["protocol_errors"] = 0 if record.get("error") else None
    row["phase"] = None
    for key in USAGE:
        row[key] = number(record.get(key))
    row["provider_total_tokens"] = row["total_tokens"]
    inp, out = row["input_tokens"], row["output_tokens"]
    row["total_tokens"] = inp + out if inp is not None and out is not None else None
    row["total_tokens_definition"] = "input_tokens + output_tokens"
    row["cost_usd"] = None
    return row


def usage_totals(calls: list[dict], *, complete: bool) -> dict:
    result = {"calls_completed": len(calls), "usage_coverage_complete": complete}
    for key in USAGE:
        values = [number(call.get(key)) for call in calls]
        known = [value for value in values if value is not None]
        subtotal = sum(known) if known else None
        result[key] = subtotal if complete and calls and len(known) == len(calls) else None
        result["known_" + key] = subtotal
        result[key + "_known_calls"] = len(known)
    result["usage_complete"] = complete and bool(calls) and all(
        all(number(call.get(k)) is not None for k in ("input_tokens", "output_tokens")) for call in calls)
    result["cost_usd"] = None
    return result


def model_totals(calls: list[dict], runs: list[dict], *, include_idle_models: bool = True) -> list[dict]:
    matrix_runs = [r for r in runs if r["scope"] == "matrix"]
    terminal = bool(matrix_runs) and all(r["completed"] for r in matrix_runs)
    coverage = terminal and all(r["usage_coverage_complete"] for r in matrix_runs)
    status = "in_progress" if not terminal else ("completed" if coverage else "incomplete_ledger")
    rows = []
    observed_models = {c.get("model_requested") for c in calls
                       if c["scope"] == "matrix" and isinstance(c.get("model_requested"), str)}
    models = [m for m in MODELS if include_idle_models or m in observed_models]
    models += sorted(observed_models - set(MODELS))
    for model in models:
        selected = [c for c in calls if c["scope"] == "matrix" and c.get("model_requested") == model]
        role_counts = {role: sum(c.get("role") == role for c in selected) for role in ROLES}
        row = {"model_requested": model, "scope": "all_matrix_roles", "matrix_status": status,
               "calls": len(selected) if coverage else None, "known_calls": len(selected),
               "role_call_counts": role_counts, "roles_observed": [r for r, n in role_counts.items() if n],
               "known_call_errors": sum(c["status"] == "error" for c in selected),
               "untracked_agent_overhead_included": False,
               **usage_totals(selected, complete=coverage)}
        for target, source in (("model_call_wall_seconds", "wall_seconds"),
                               ("protocol_errors", "protocol_errors"),
                               ("external_tool_calls", "external_tool_calls")):
            values = [number(c.get(source)) for c in selected]
            known = [v for v in values if v is not None]
            subtotal = sum(known) if known else None
            row[target] = subtotal if coverage and selected and len(known) == len(selected) else None
            row["known_" + target] = subtotal
            row[target + "_known_calls"] = len(known)
        rows.append(row)
    return rows


def fallback_schedule() -> list[dict]:
    rows = []
    for task in TASKS:
        for model in MODELS:
            rows.append({"run_id": f"{task}__team__{model}", "task_id": task,
                         "condition": "team", "executor": model})
        rows.append({"run_id": f"{task}__solo__gpt-5.6-sol", "task_id": task,
                     "condition": "solo", "executor": "gpt-5.6-sol"})
    random.Random(20260903).shuffle(rows)
    return rows


def phase_snapshot(folder: Path, result: dict, warnings: list[str]) -> dict[str, dict]:
    phases = {p["phase"]: p for p in result.get("phases", [])
              if isinstance(p, dict) and isinstance(p.get("phase"), str)}
    base = folder / "phases"
    if base.is_dir():
        for directory in sorted(p for p in base.iterdir() if p.is_dir()):
            data = obj(read_json(directory / "phase.json", warnings))
            if data:
                phases.setdefault(directory.name, data)
            elif directory.name not in phases:
                steps = [obj(read_json(p, warnings)) for p in directory.glob("turn_*.json")]
                phases[directory.name] = {
                    "phase": directory.name, "role": PHASE_ROLES.get(directory.name), "status": "running",
                    "tool_calls": sum(len(s.get("tool_results", [])) for s in steps),
                    "protocol_errors": sum("protocol_error" in s for s in steps), "partial_snapshot": True,
                }
    return phases


def phase_metrics(phases: list[dict], *, finalized: bool) -> dict:
    tools = [number(p.get("tool_calls")) for p in phases]
    errors = [number(p.get("protocol_errors")) for p in phases]
    complete = finalized and all(not p.get("partial_snapshot") for p in phases)
    return {
        "tool_calls": sum(tools) if complete and all(v is not None for v in tools) else None,
        "protocol_errors": sum(errors) if complete and all(v is not None for v in errors) else None,
        "tool_calls_observed": sum(v for v in tools if v is not None),
        "protocol_errors_observed": sum(v for v in errors if v is not None),
        "phase_statuses": [str(p.get("phase")) + ":" + str(p.get("status")) for p in phases],
    }


def attach_call_actions(folder: Path, calls: list[dict], warnings: list[str]) -> None:
    lookup = {c["call_id"]: c for c in calls}
    for path in sorted((folder / "phases").glob("*/turn_*.json")):
        step = obj(read_json(path, warnings))
        call = lookup.get(step.get("call_id"))
        if call is None:
            continue
        tools = step.get("tool_results", [])
        call["phase"] = path.parent.name
        call["external_tool_calls"] = len(tools) if isinstance(tools, list) else None
        call["external_tool_error_count"] = sum(obj(t.get("result")).get("exit_code") not in (None, 0)
                                                for t in tools if isinstance(t, dict)) if isinstance(tools, list) else None
        call["protocol_errors"] = int("protocol_error" in step)


def elapsed(started: Any, now: datetime) -> float | None:
    if not isinstance(started, str):
        return None
    try:
        value = datetime.fromisoformat(started.replace("Z", "+00:00"))
        return round(max(0, (now - value).total_seconds()), 3) if value.tzinfo else None
    except ValueError:
        return None


def build_snapshot(root: Path) -> dict:
    warnings: list[str] = []
    now = datetime.now(timezone.utc)
    manifest = obj(read_json(root / "manifest.json", warnings))
    native_followup = manifest.get("experiment_kind") == "native_tools_followup"
    schedule = manifest.get("schedule")
    if not isinstance(schedule, list):
        if native_followup:
            schedule = []
            warnings.append("原生工具复测 manifest 缺少有效 schedule：不推定主矩阵条件，等待正式复测清单。")
        else:
            schedule = fallback_schedule()
            warnings.append("manifest 尚未可读：矩阵使用已知两题 × 六条件，等待正式 manifest。")
    entries = {e["run_id"]: dict(e) for e in schedule if isinstance(e, dict) and isinstance(e.get("run_id"), str)}
    matrix_ids = set(entries)
    results = {}
    results_dir = root / "results"
    if results_dir.is_dir():
        for folder in sorted(p for p in results_dir.iterdir() if p.is_dir()):
            result = obj(read_json(folder / "result.json", warnings))
            if result:
                results[folder.name] = result
            if folder.name not in entries:
                started = obj(read_json(folder / "started.json", warnings))
                if result or started:
                    entries[folder.name] = {**started, **result, "run_id": folder.name}
    completed, started_calls, duplicates = read_ledger(root / "calls.jsonl", warnings)
    calls = [normalize_call(value, matrix_ids) for value in completed.values()]
    for call in calls:
        call["experiment_kind"] = manifest.get("experiment_kind") or "main_matrix"
        call["experiment_label"] = manifest.get("experiment_label")
        call["tool_channel_declared"] = ("glm_native_tools" if native_followup and str(call.get("model_requested", "")).startswith("glm-")
                                         else "text_json")
    calls.sort(key=lambda c: (str(c.get("started_at") or ""), c["call_id"]))
    runs, roles = [], []
    for run_id, entry in entries.items():
        folder = results_dir / run_id
        result = results.get(run_id, {})
        started = obj(read_json(folder / "started.json", warnings))
        finalized = bool(result) and bool(result.get("completed_at"))
        records = [c for c in calls if c.get("run_id") == run_id]
        attach_call_actions(folder, records, warnings)
        pending = [c for key, c in started_calls.items() if key not in completed and c.get("run_id") == run_id]
        status = result.get("status") or ("running" if folder.is_dir() or started or records or pending else "pending")
        expected_ids = result.get("call_ids")
        expected_count = number(result.get("call_count"))
        coverage = (finalized and isinstance(expected_ids, list)
                    and set(expected_ids) == {c["call_id"] for c in records}
                    and (expected_count is None or expected_count == len(records)))
        if finalized and not coverage:
            warnings.append(f"{run_id} 的 result call_ids 与 completed ledger 不完整匹配，用量总计保留 null。")
        score = obj(result.get("score")) or obj(obj(result.get("grading")).get("raw_score"))
        grading = obj(result.get("grading"))
        secondary = obj(score.get("secondary"))
        passed = boolean(score.get("pass"))
        checks = number(secondary.get("checks_passed"))
        count = number(secondary.get("checks_total", secondary.get("total_checks")))
        ratio = checks / count if checks is not None and count is not None and count > 0 else None
        clean_score = (status == "scored" and passed is not None and grading.get("exit_code") == 0
                       and grading.get("timed_out") is False and not grading.get("score_parse_error"))
        attestation = obj(result.get("attestation")) or obj(read_json(folder / "submission" / "attestation.json", warnings))
        control = obj(result.get("control")) or obj(read_json(folder / "control-plane" / "result.json", warnings))
        oracle = obj(control.get("oracle"))
        phases = phase_snapshot(folder, result, warnings)
        condition = entry.get("condition") or result.get("condition")
        executor = entry.get("executor") or result.get("executor")
        row = {
            "run_id": run_id, "scope": "matrix" if run_id in matrix_ids else "unscored_other",
            "task_id": entry.get("task_id") or result.get("task_id"), "condition": condition, "executor": executor,
            "status": status, "completed": finalized, "scored_valid": clean_score,
            "raw_pass": passed, "raw_primary_success": obj(score.get("primary")).get("success"),
            "checks_passed": checks, "checks_total": count, "checks_ratio": ratio,
            "raw_partial_score": number(secondary.get("partial_score")), "failure_modes": score.get("failure_modes"),
            "attestation_verdict": attestation.get("verdict"), "attestation_error": attestation.get("error"),
            "attestation_false_accept": (attestation.get("verdict") == "pass" and passed is False) if clean_score else None,
            "control_accepted": boolean(control.get("control_accepted")),
            "control_oracle_verified": boolean(oracle.get("verified")), "control_oracle_reason": oracle.get("reason"),
            "control_replay_passed": boolean(control.get("invariant_replay_passed")),
            "control_role_evidence_complete": boolean(control.get("role_evidence_complete")),
            "native_orchestrator_exercised": boolean(control.get("native_orchestrator_exercised")),
            "control_error": control.get("error"), "started_at": result.get("started_at") or started.get("started_at"),
            "completed_at": result.get("completed_at"), "wall_seconds": number(result.get("wall_seconds")),
            "calls_expected": expected_count, "calls_pending": len(pending),
            "call_error_count": sum(c["status"] == "error" for c in records),
            "grader_exit_code": grading.get("exit_code"), "grader_timed_out": grading.get("timed_out"),
            "grader_parse_error": grading.get("score_parse_error"), "error": result.get("error"),
            "control_cleanup_error": result.get("control_cleanup_error"),
            "sandbox_cleanup_error": result.get("sandbox_cleanup_error"),
            "result_path": str(folder / "result.json") if result else None,
            **usage_totals(records, complete=coverage), **phase_metrics(list(phases.values()), finalized=finalized),
        }
        row["elapsed_seconds_so_far"] = elapsed(row["started_at"], now) if not finalized else None
        expected_roles = ("oracle",) if condition == "solo" else ("planner", "executor", "verifier")
        for role in expected_roles:
            role_calls = [c for c in records if c.get("role") == role]
            role_phases = [p for name, p in phases.items() if p.get("role", PHASE_ROLES.get(name)) == role]
            role_model = {"planner": "gpt-5.6-sol", "executor": executor,
                          "verifier": "gpt-5.6-terra", "oracle": "gpt-5.6-sol"}[role]
            role_row = {"run_id": run_id, "scope": row["scope"], "task_id": row["task_id"],
                        "condition": condition, "executor": executor, "role": role, "model_requested": role_model,
                        "run_status": status, "run_completed": finalized,
                        "models_reported": sorted({c["model_reported"] for c in role_calls if isinstance(c.get("model_reported"), str)}) or None,
                        "tiers_reported": sorted({c["service_tier_reported"] for c in role_calls if isinstance(c.get("service_tier_reported"), str)}) or None,
                        "model_reported_calls": sum(c.get("model_reported") is not None for c in role_calls),
                        "tier_reported_calls": sum(c.get("service_tier_reported") is not None for c in role_calls),
                        "call_error_count": sum(c["status"] == "error" for c in role_calls),
                        **usage_totals(role_calls, complete=coverage),
                        **phase_metrics(role_phases, finalized=finalized)}
            wall = [number(c.get("wall_seconds")) for c in role_calls]
            role_row["model_wall_seconds"] = sum(wall) if coverage and wall and all(v is not None for v in wall) else None
            role_row["known_model_wall_seconds"] = sum(v for v in wall if v is not None) if any(v is not None for v in wall) else None
            role_row["team_token_share"] = role_row["total_tokens"] / row["total_tokens"] if (
                role_row["total_tokens"] is not None and row["total_tokens"] is not None and row["total_tokens"] > 0) else None
            roles.append(role_row)
            for key in (*USAGE, "calls_completed", "tool_calls", "protocol_errors", "model_wall_seconds"):
                row[role + "_" + key] = role_row.get(key)
        e_tokens = row.get("executor_total_tokens")
        row["executor_team_token_share"] = e_tokens / row["total_tokens"] if (
            condition != "solo" and e_tokens is not None and row["total_tokens"] is not None and row["total_tokens"] > 0) else None
        runs.append(row)
    preflight, _, preflight_duplicates = read_ledger(root / "preflight" / "calls.jsonl", warnings)
    return {"root": str(root), "generated_at": now.isoformat(), "manifest": manifest, "calls": calls,
            "runs": runs, "roles": roles, "models": model_totals(calls, runs, include_idle_models=not native_followup),
            "matrix_ids": matrix_ids, "duplicate_completed_records": duplicates,
            "preflight_completed_calls": len(preflight), "preflight_duplicate_completed_records": preflight_duplicates,
            "warnings": warnings}


def cell(value: Any) -> Any:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value


def atomic_text(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8-sig" if path.suffix == ".csv" else "utf-8", newline="")
    temporary.replace(path)


def write_csv(path: Path, rows: list[dict], default_fields: tuple[str, ...]) -> None:
    fields = list(default_fields)
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    writer.writerows({key: cell(row.get(key)) for key in fields} for row in rows)
    atomic_text(path, buffer.getvalue())


def display(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:,.{digits}f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def label(model: Any) -> str:
    return str(model or "—").replace("gpt-5.6-", "").replace("glm-5.3-flash", "GLM-5.3-Flash").replace("glm-5.3", "GLM-5.3")


def complete_sum(rows: list[dict], key: str, *, require_usage: bool = False) -> int | float | None:
    if not rows or not all(r["completed"] for r in rows):
        return None
    if require_usage and not all(r["usage_complete"] for r in rows):
        return None
    values = [number(r.get(key)) for r in rows]
    return sum(values) if all(v is not None for v in values) else None


def matrix_times(rows: list[dict]) -> tuple[str | None, str | None, float | None]:
    if not rows or not all(r["completed"] for r in rows):
        return None, None, None
    try:
        starts = [datetime.fromisoformat(r["started_at"].replace("Z", "+00:00")) for r in rows]
        ends = [datetime.fromisoformat(r["completed_at"].replace("Z", "+00:00")) for r in rows]
        if not all(t.tzinfo is not None for t in starts + ends):
            return None, None, None
        earliest, latest = min(starts), max(ends)
        return earliest.isoformat(), latest.isoformat(), max(0, (latest - earliest).total_seconds())
    except (AttributeError, KeyError, TypeError, ValueError):
        return None, None, None


def make_report(snapshot: dict) -> str:
    runs = [r for r in snapshot["runs"] if r["scope"] == "matrix"]
    manifest = snapshot["manifest"]
    native_followup = manifest.get("experiment_kind") == "native_tools_followup"
    title = manifest.get("experiment_label") or ("GLM 原生工具探索性复测" if native_followup else "Agent Team 两题 pilot")
    pairs = list(dict.fromkeys((r["condition"], r["executor"]) for r in runs))
    pairs.sort(key=lambda pair: (pair[0] == "solo", MODELS.index(pair[1]) if pair[1] in MODELS else len(MODELS), str(pair[0])))
    task_ids = {r["task_id"] for r in runs}
    has_solo = any(condition == "solo" for condition, _ in pairs)
    budget_note = ("固定 Planner=Sol、Verifier=Terra，仅改变 Executor；另有 Solo Sol。两题分别是 SPEC5 generated seed 0 和 INT1 tracked static。Solo 与 Team 的模型调用**上限**均为 18，不是实际 token 或费用匹配。"
                   if has_solo else "固定 Planner=Sol、Verifier=Terra，仅比较实际 manifest 中列出的 Executor 条件。两题为 SPEC5 generated seed 0 和 INT1 tracked static，沿用每个 Team 最多 18 次调用的预算；本复测没有新的 Solo 条件。")
    protocol_heading = "已知通道协议错误" if native_followup else "已知 JSON 协议错误"
    completed = sum(r["completed"] for r in runs)
    scored = sum(r["scored_valid"] for r in runs)
    successes = sum(r["scored_valid"] and r["raw_pass"] is True for r in runs)
    running = sum(not r["completed"] and r["status"] == "running" for r in runs)
    errors = sum(r["completed"] and not r["scored_valid"] for r in runs)
    state = "已完成" if completed == len(runs) and runs else "进行中"
    matrix_tokens = complete_sum(runs, "total_tokens", require_usage=True)
    matrix_wall_sum = complete_sum(runs, "wall_seconds")
    earliest, latest, matrix_span = matrix_times(runs)
    lines = [f"# {display(title)} 结果", "", f"更新时间：{snapshot['generated_at']}。状态：**{state}**。"]
    if native_followup:
        lines += ["**这是看到主矩阵协议错误后开展的探索性复测。结果独立保存，不合并、不替换主矩阵原始分数，也不作为预注册主结果。** GLM 使用原生 tool calling，GPT Planner/Verifier 继续使用文本 JSON 工具通道；两种协议的错误不能套用同一纯文本 JSON 诊断。", ""]
    lines += [
             f"矩阵终止 {completed}/{len(runs)}，有效评分 {scored}，其中通过 {successes}；进行中 {running}，已终止但非有效评分 {errors}。所有条件与失败均保留。", "",
             f"完整矩阵总 token：**{display(matrix_tokens)}**；实际时间跨度（最早 start → 最晚 end）：**{display(matrix_span)} 秒**；各 run wall 秒之和：**{display(matrix_wall_sum)} 秒**。后两项分别表示并发实验的历时与累计运行时间，不能混用。",
             f"矩阵起止 UTC：{display(earliest)} → {display(latest)}。矩阵尚未全部终止时，完整时间统计保留 null；总 token 还要求全部调用用量完整。", "",
             budget_note, "",
             "| Executor / 条件 | 通过题数 | 已终止 | 有效评分 | 非有效评分 | 完整团队/solo token | Executor token | 两题累计 wall 秒 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for condition, model in pairs:
        selected = [r for r in runs if r["condition"] == condition and r["executor"] == model]
        passed = sum(r["scored_valid"] and r["raw_pass"] is True for r in selected)
        done = sum(r["completed"] for r in selected)
        valid = sum(r["scored_valid"] for r in selected)
        enough = len(selected) == len(task_ids) and {r["task_id"] for r in selected} == task_ids
        tokens = complete_sum(selected, "total_tokens", require_usage=True) if enough else None
        e_tokens = complete_sum(selected, "executor_total_tokens", require_usage=True) if enough and condition != "solo" else None
        walls = complete_sum(selected, "wall_seconds") if enough else None
        lines.append(f"| {label(model)} / {condition} | {passed}/{len(selected)} | {done}/{len(selected)} | {valid} | {done-valid} | {display(tokens)} | {display(e_tokens)} | {display(walls)} |")
    lines += ["", "待完成条件不作失败断言；以上通过题数是当前已确认数，固定模型顺序展示，不按成绩排序。首表 token 仅在该条件两题均终止且 usage 完整时汇总，否则为 null（显示 —）；wall 时间独立按已知时间汇总，不因 token 缺失而隐藏；Solo 的 Executor token 不适用。", "",
              "下面按实际 `model_requested` 汇总该模型在全部矩阵条件中担任 P/E/V/S 的调用。它与首表按 Executor 条件计算的整队成本不同；此表为已完成调用的**已知小计**，矩阵进行中时 models.csv 的完整合计字段保持 null。", "",
              f"| 实际调用模型 | 状态 | 实际用途（调用数） | 已完成调用 | 已知 token 小计 | token 已知调用覆盖 | 已知调用秒之和 | {protocol_heading} |",
              "|---|---|---|---:|---:|---:|---:|---:|"]
    role_labels = {"planner": "P", "executor": "E", "verifier": "V", "oracle": "S"}
    for model in snapshot["models"]:
        purpose = "/".join(f"{role_labels[r]}:{n}" for r, n in model["role_call_counts"].items() if n) or "尚无调用"
        model_state = {"in_progress": "进行中", "completed": "已完成", "incomplete_ledger": "ledger 不完整"}[model["matrix_status"]]
        lines.append(f"| {label(model['model_requested'])} | {model_state} | {purpose} | {model['known_calls']} | {display(model['known_total_tokens'])} | {model['total_tokens_known_calls']}/{model['known_calls']} | {display(model['known_model_call_wall_seconds'])} | {display(model['known_protocol_errors'])} |")
    lines += ["", "模型调用秒数是各次请求耗时之和，**不等于实验历时**；调用可并发，且不包含两次调用之间的工具和控制面时间。input/output/cached/reasoning 的完整值、已知小计及覆盖数均在 models.csv，未知项不补 0。", "",
              "| 任务 | 条件 / E | 状态 | raw pass | 检查 | attestation | CP 验证 / 接受 | 调用 | P/E/V/S token | 总 token | E 占比 | 总秒 |",
              "|---|---|---|---|---:|---|---|---:|---|---:|---:|---:|"]
    for r in runs:
        checks = f"{display(r['checks_passed'])}/{display(r['checks_total'])}"
        tokens = "/".join(display(r.get(role + "_total_tokens")) for role in ROLES)
        share = f"{r['executor_team_token_share']:.1%}" if r["executor_team_token_share"] is not None else "—"
        lines.append("| " + " | ".join([
            str(r["task_id"]).split("_", 1)[0], f"{r['condition']} / {label(r['executor'])}", display(r["status"]),
            display(r["raw_pass"]), checks, display(r["attestation_verdict"]),
            display(r["control_oracle_verified"]) + " / " + display(r["control_accepted"]),
            display(r["calls_completed"]), tokens, display(r["total_tokens"]), share, display(r["wall_seconds"]),
        ]) + " |")
    lines += ["", "P/E/V/S 分别为 Planner、Executor、Verifier、Solo。进行中或用量证据不完整时总量显示 —；CSV 中为字面量 null，known_* 仅表示已知小计，不能当完整总量。调用数为已完成 ledger 条数，包含失败调用。", "",
              "主指标是原版 `raw_score.pass`，不是 shell 退出码、`status=scored` 或控制面接受。检查精确比例见 `checks_ratio`；`raw_partial_score` 保留官方四舍五入结果。SPEC5 为 19 项，INT1 为 12 项且 attestation 占一项；buggy baseline 分别为 **0/19** 和 **1/12（0.08）**。", ""]
    failures = [r for r in runs if r["completed"] and (not r["scored_valid"] or r["raw_pass"] is False
                or r["control_accepted"] is False or r["control_cleanup_error"] or r["sandbox_cleanup_error"])]
    if failures:
        lines += ["| 异常或失败条件 | 记录 |", "|---|---|"]
        for r in failures:
            detail = {key: r[key] for key in ("failure_modes", "error", "grader_timed_out", "grader_parse_error",
                      "control_oracle_reason", "control_error", "control_cleanup_error", "sandbox_cleanup_error")
                      if r.get(key) not in (None, False, [], {})}
            for key, value in list(detail.items()):
                if isinstance(value, dict):
                    detail[key] = {k: value[k] for k in ("type", "message") if k in value}
            short = json.dumps(detail, ensure_ascii=False)
            if len(short) > 450:
                short = short[:450] + "…（完整信息见 runs.csv）"
            lines.append(f"| {display(r['run_id'])} | {display(short)} |")
        lines.append("")
    calls = snapshot["calls"]
    matrix_calls = [c for c in calls if c["scope"] == "matrix"]
    unknown_models = sum(c.get("model_reported") is None for c in matrix_calls)
    unknown_tiers = sum(c.get("service_tier_reported") is None for c in matrix_calls)
    ledger_name = "本探索性复测 ledger" if native_followup else "主 ledger"
    tool_note = ("本复测 GLM 使用 API 原生 tools，GPT 使用 Codex CLI 的文本 JSON 通道。external_tool_calls 表示实际角色工具动作；native_tools_used 保留调用 ledger 原字段，不把合法 GLM 原生工具请求当作 CLI 越权。protocol_errors 按本通道原记录汇总，不把纯文本 JSON 格式失败的解释套到 GLM 原生结果。"
                 if native_followup else "calls.csv 的 external_tool_calls 是本实验 JSON 工具动作，native_tools_used 是被禁止的 CLI 原生工具，两者分开。")
    lines += [f"{ledger_name} 去重后 completed 调用 {len(calls)}，其中矩阵 {len(matrix_calls)}，矩阵外 {len(calls)-len(matrix_calls)}；重复 completed 行 {snapshot['duplicate_completed_records']}。独立 `preflight/calls.jsonl` 有 {snapshot['preflight_completed_calls']} 次未计分调用，未加入矩阵或本报告用量。任务/隔离烟测同样不计分。", "",
              f"矩阵调用中，服务端未报告实际 model 的有 {unknown_models}/{len(matrix_calls)}，未报告 tier 的有 {unknown_tiers}/{len(matrix_calls)}；未报告字段保持 null。请求 max/fast 不等于服务端确认。GPT 经 Codex CLI，包含其系统提示开销；GLM 经 API，比较的是本次部署栈。", "",
              "`input_tokens` 已包含 cached input，`output_tokens` 已包含 reasoning；总量仅 input + output，不能再次加缓存或推理 token。完整团队与各角色 input/output/cached/reasoning、调用、工具和协议错误见 CSV。" + tool_note + "缺失值不当作已知 0；费用缺乏可靠依据，`cost_usd=null`。", "",
              "主导、搭建和分析 agent 的开销不在本候选调用 ledger 中，因此不包含在这些总量里。这部分缺少 telemetry，调用数、token、耗时和费用均为未知，不能填 0。", "",
              "这只是两题工程 pilot，不能作模型综合排名或异构协作因果结论。此次接入原生 DPswarm ControlPlane 的工作项、usage、submission 与评分证据；没有验证原生 Orchestrator 自动 fission/router 或 DSH 全链路。控制面验证、接受和 grader pass 分开记录。", ""]
    output = Path(snapshot.get("output_dir", snapshot["root"]))
    links = [f"[{name}](<{(output / name).as_posix()}>)" for name in ("calls.csv", "runs.csv", "roles.csv", "models.csv")]
    evidence_root = Path(snapshot["root"])
    if native_followup and not (evidence_root / "task_audit" / "TASK_SELECTION.md").is_file():
        evidence_root = evidence_root.parent
    links.append(f"[任务预检](<{(evidence_root / 'task_audit' / 'TASK_SELECTION.md').as_posix()}>)")
    lines.append("文件：" + " · ".join(links) + "。")
    if snapshot["warnings"]:
        lines += ["", "本次读取提示（下次运行会重新读取）："]
        lines += ["- " + display(w) for w in snapshot["warnings"]]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="Frozen experiment directory")
    parser.add_argument("--output-dir", type=Path, help="Defaults to root")
    parser.add_argument("--check-only", action="store_true", help="Read and validate current snapshot without writing reports")
    args = parser.parse_args()
    root = args.root.resolve()
    snapshot = build_snapshot(root)
    snapshot["output_dir"] = str((args.output_dir or root).resolve())
    report = make_report(snapshot)
    if not args.check_only:
        output = (args.output_dir or root).resolve()
        output.mkdir(parents=True, exist_ok=True)
        write_csv(output / "calls.csv", snapshot["calls"], ("call_id", "run_id", "scope", "task_id", "role", "status"))
        write_csv(output / "runs.csv", snapshot["runs"], ("run_id", "scope", "task_id", "condition", "executor", "status"))
        write_csv(output / "roles.csv", snapshot["roles"], ("run_id", "scope", "task_id", "condition", "executor", "role"))
        write_csv(output / "models.csv", snapshot["models"], ("model_requested", "scope", "matrix_status", "calls", "known_calls"))
        atomic_text(output / "REPORT.md", report)
    matrix = [r for r in snapshot["runs"] if r["scope"] == "matrix"]
    print(json.dumps({"check_only": args.check_only, "matrix_runs": len(matrix),
                      "completed_runs": sum(r["completed"] for r in matrix),
                      "scored_runs": sum(r["scored_valid"] for r in matrix),
                      "completed_calls": len(snapshot["calls"]), "role_rows": len(snapshot["roles"]),
                      "model_rows": len(snapshot["models"]),
                      "warnings": snapshot["warnings"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
