"""Post-hoc, offline diagnostics of the frozen text-only tool protocol.

Reads completed calls and phase turn evidence. It never executes proposed model
tools, changes scores, retries requests, or imports the live experiment driver.
Only its two named diagnostic reports are written when explicitly run.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable


FENCE = re.compile(r"\A\s*```[ \t]*(?:json)?[ \t]*\r?\n(?P<body>[\s\S]*?)\r?\n```[ \t]*\s*\Z", re.IGNORECASE)
EXPECTED_MODELS = ("glm-5.3", "glm-5.3-flash", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")


def sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def relative(path: str | Path | None, root: Path) -> str | None:
    if not path:
        return None
    value = Path(path)
    if not value.is_absolute():
        value = root / value
    try:
        return value.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return value.as_posix()


def load_frozen_parser(root: Path) -> tuple[Callable[[str], Any], dict[str, Any]]:
    """Compile only the already-frozen parse_action function, never main/imports.

    Model output is passed solely to json.loads by this function. No model text
    is compiled, evaluated, executed, or used as Python source.
    """
    path = root / "run_experiment.py"
    payload = path.read_bytes()
    tree = ast.parse(payload.decode("utf-8-sig"), filename=str(path))
    node = next((item for item in tree.body if isinstance(item, ast.FunctionDef)
                 and item.name == "parse_action"), None)
    if node is None:
        raise ValueError("Frozen driver has no parse_action function")
    namespace: dict[str, Any] = {"json": json, "__builtins__": {
        "isinstance": isinstance, "dict": dict, "list": list, "str": str,
        "len": len, "any": any, "ValueError": ValueError, "TypeError": TypeError}}
    code = compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                   filename=str(path), mode="exec")
    exec(code, namespace)
    manifest_path = root / "manifest.json"
    expected = None
    if manifest_path.is_file():
        expected = json.loads(manifest_path.read_text(encoding="utf-8-sig")).get("program_sha256", {}).get("run_experiment.py")
    actual = sha_bytes(payload)
    return namespace["parse_action"], {
        "source_path": "run_experiment.py", "source_sha256": actual,
        "frozen_manifest_sha256": expected,
        "matches_frozen_manifest": actual == expected if expected is not None else None,
        "function": "parse_action", "source_line": node.lineno,
        "method": "AST extraction of the frozen parser only; no driver imports or model output execution",
    }


def native_shape(value: Any) -> list[str]:
    """Shape observations only; these do not assert native execution occurred."""
    found: set[str] = set()
    if isinstance(value, list):
        if value and all(isinstance(item, dict) for item in value):
            if any(item.get("type") in ("function_call", "tool_use") or "function" in item for item in value):
                found.add("native_tool_object_array")
        for item in value[:16]:
            if isinstance(item, dict) and item.get("type") in ("tool_use", "function_call"):
                found.add("native_tool_object")
        return sorted(found)
    if not isinstance(value, dict):
        return []
    if value.get("type") in ("function_call", "tool_use", "custom_tool_call"):
        found.add("native_tool_object")
    if isinstance(value.get("tool_calls"), list):
        found.add("openai_tool_calls_field")
    if isinstance(value.get("function"), dict) and "name" in value["function"]:
        found.add("openai_function_object")
    if "name" in value and "input" in value and value.get("type") != "final":
        found.add("tool_name_input_object")
    for key in ("calls", "content", "output"):
        parts = value.get(key)
        if isinstance(parts, list):
            for part in parts[:16]:
                if isinstance(part, dict):
                    if isinstance(part.get("function"), dict):
                        found.add("nested_openai_function_object")
                    if part.get("type") in ("tool_use", "function_call", "custom_tool_call"):
                        found.add("nested_native_tool_object")
    return sorted(found)


def tool_format_issues(value: Any) -> list[str]:
    """Identify required text-protocol fields without guessing intended actions."""
    if not isinstance(value, dict) or value.get("type") != "tool_calls":
        return []
    calls = value.get("calls")
    if not isinstance(calls, list):
        return ["calls_missing_or_not_array"]
    issues = set()
    if not 1 <= len(calls) <= 8:
        issues.add("tool_batch_size_outside_1_to_8")
    for item in calls:
        if not isinstance(item, dict):
            issues.add("call_not_object")
            continue
        if not isinstance(item.get("name"), str):
            issues.add("tool_name_missing_or_not_string")
        if not isinstance(item.get("arguments"), dict):
            issues.add("arguments_missing_or_not_object")
            if any(key in item for key in ("cmd", "path", "content", "to")):
                issues.add("argument_fields_at_call_top_level")
    return sorted(issues)


def syntax_details(error: json.JSONDecodeError) -> dict[str, Any]:
    message = error.msg
    if "Invalid \\escape" in message or "Invalid \\u" in message:
        category = "json_invalid_escape"
    elif "Invalid control character" in message:
        category = "json_unescaped_control_character"
    elif "Unterminated string" in message:
        category = "json_unterminated_string"
    elif "property name enclosed in double quotes" in message:
        category = "json_property_quotes_or_delimiter"
    elif "delimiter" in message:
        category = "json_quote_or_delimiter_syntax"
    elif "Extra data" in message:
        category = "json_trailing_data"
    else:
        category = "json_other_syntax"
    return {"category": category, "message": message, "line": error.lineno,
            "column": error.colno, "character_offset": error.pos,
            "quote_or_escape_family": category in {
                "json_invalid_escape", "json_unescaped_control_character", "json_unterminated_string",
                "json_property_quotes_or_delimiter", "json_quote_or_delimiter_syntax"}}


def classify_text(text: Any, parser: Callable[[str], Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "strict_json_parseable": False, "strict_protocol_valid": False,
        "pure_markdown_fence": False, "fenced_inner_json_parseable": None,
        "fenced_inner_protocol_valid": None, "native_format_shapes": [],
        "tool_format_issues": [],
        "syntax_error": None, "schema_error": None, "primary_category": None,
        "quote_or_escape_syntax": False,
    }
    if not isinstance(text, str):
        result["primary_category"] = "non_string_output"
        return result
    if not text.strip():
        result["primary_category"] = "empty_output"
        return result
    fence = FENCE.fullmatch(text)
    result["pure_markdown_fence"] = fence is not None
    value = None
    try:
        value = json.loads(text)
        result["strict_json_parseable"] = True
    except json.JSONDecodeError as exc:
        result["syntax_error"] = syntax_details(exc)
    try:
        parser(text)
        result["strict_protocol_valid"] = True
    except (ValueError, TypeError) as exc:
        if result["strict_json_parseable"]:
            result["schema_error"] = str(exc)[:240]
    if fence:
        body = fence.group("body")
        try:
            inner_value = json.loads(body)
            result["fenced_inner_json_parseable"] = True
            result["native_format_shapes"] = native_shape(inner_value)
            result["tool_format_issues"] = tool_format_issues(inner_value)
            try:
                parser(body)
                result["fenced_inner_protocol_valid"] = True
                result["primary_category"] = "pure_fenced_json_valid_protocol"
            except (ValueError, TypeError) as exc:
                result["fenced_inner_protocol_valid"] = False
                result["schema_error"] = str(exc)[:240]
                result["primary_category"] = "pure_fenced_json_wrong_schema"
        except json.JSONDecodeError as exc:
            result["fenced_inner_json_parseable"] = False
            result["fenced_inner_protocol_valid"] = False
            result["inner_syntax_error"] = syntax_details(exc)
            result["quote_or_escape_syntax"] = result["inner_syntax_error"]["quote_or_escape_family"]
            result["primary_category"] = "pure_fence_with_invalid_json"
    elif result["strict_protocol_valid"]:
        result["primary_category"] = "valid_protocol"
    elif result["strict_json_parseable"]:
        result["native_format_shapes"] = native_shape(value)
        result["tool_format_issues"] = tool_format_issues(value)
        result["primary_category"] = "native_tool_json_format" if result["native_format_shapes"] else "wrong_protocol_schema"
    else:
        if re.match(r"\s*(?:<tool_call>|<function[ =]|<\|(?:tool|function))", text):
            result["native_format_shapes"] = ["native_like_text_marker"]
            result["primary_category"] = "native_like_text_format"
        elif text.lstrip().startswith("```"):
            result["primary_category"] = "markdown_fence_with_extra_text_or_non_json_label"
        elif result["syntax_error"] and result["syntax_error"]["character_offset"] == 0 and text.lstrip()[0] not in '{["-0123456789tfn':
            result["primary_category"] = "non_json_prose_or_code"
        else:
            result["primary_category"] = result["syntax_error"]["category"] if result["syntax_error"] else "unclassified_parse_error"
        if result["syntax_error"]:
            result["quote_or_escape_syntax"] = result["syntax_error"]["quote_or_escape_family"]
    return result


def read_inputs(root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]], dict[str, Any]]:
    calls_path = root / "calls.jsonl"
    payload = calls_path.read_bytes() if calls_path.is_file() else b""
    lines = payload.decode("utf-8-sig").splitlines()
    calls: dict[str, dict[str, Any]] = {}
    started: set[str] = set()
    warnings = []
    duplicate_completed = 0
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            warnings.append({"path": "calls.jsonl", "line": number,
                             "type": "incomplete_last_line" if number == len(lines) else "invalid_jsonl_line",
                             "error": str(exc)[:160]})
            continue
        if not isinstance(row, dict) or not isinstance(row.get("call_id"), str):
            warnings.append({"path": "calls.jsonl", "line": number, "type": "invalid_call_record"})
            continue
        if row.get("event") == "started":
            started.add(row["call_id"])
        if row.get("event") != "completed":
            continue
        if row["call_id"] in calls:
            duplicate_completed += 1
            if calls[row["call_id"]]["record"] != row:
                warnings.append({"path": "calls.jsonl", "line": number,
                                 "call_id": row["call_id"], "type": "conflicting_duplicate_completed_call"})
            continue
        calls[row["call_id"]] = {"record": row, "line": number}
    turns: dict[str, list[dict[str, Any]]] = defaultdict(list)
    phase_checks = []
    paths = sorted((root / "results").glob("*/phases/*/turn_*.json"))
    for path in paths:
        try:
            row = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            warnings.append({"path": relative(path, root), "type": "unreadable_phase_turn", "error": str(exc)[:160]})
            continue
        call_id = row.get("call_id") if isinstance(row, dict) else None
        if not isinstance(call_id, str):
            warnings.append({"path": relative(path, root), "type": "missing_turn_call_id"})
            continue
        turns[call_id].append({"path": relative(path, root), "phase": path.parent.name,
                               "run_id": path.parents[2].name, "turn": row.get("turn"),
                               "protocol_error": row.get("protocol_error"),
                               "has_action": "action" in row})
    for path in sorted((root / "results").glob("*/phases/*/phase.json")):
        try:
            phase = json.loads(path.read_text(encoding="utf-8-sig"))
            ids = phase.get("call_ids", [])
            observed = sum(any(turn.get("protocol_error") for turn in turns.get(call_id, [])) for call_id in ids)
            phase_checks.append({"path": relative(path, root), "role": phase.get("role"),
                "phase": phase.get("phase"), "model": phase.get("model"), "status": phase.get("status"),
                "completed_call_count": sum(call_id in calls for call_id in ids),
                "recorded_protocol_errors": phase.get("protocol_errors"),
                "joined_turn_protocol_errors": observed,
                "counts_match": phase.get("protocol_errors") == observed})
        except (OSError, ValueError, TypeError) as exc:
            warnings.append({"path": relative(path, root), "type": "unreadable_phase_summary", "error": str(exc)[:160]})
    for call_id, matches in turns.items():
        if call_id not in calls:
            warnings.append({"call_id": call_id, "type": "phase_turn_without_completed_call",
                             "paths": [match["path"] for match in matches]})
        if len(matches) > 1:
            warnings.append({"call_id": call_id, "type": "duplicate_phase_turn_join",
                             "paths": [match["path"] for match in matches]})
    return calls, turns, {"calls_path": "calls.jsonl", "calls_snapshot_sha256": sha_bytes(payload),
        "calls_snapshot_bytes": len(payload), "calls_snapshot_lines": len(lines),
        "completed_unique_calls": len(calls), "started_without_completed": sorted(started - calls.keys()),
        "duplicate_completed_records": duplicate_completed, "phase_turn_files": len(paths),
        "phase_summary_checks": phase_checks, "warnings": warnings}


def group_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    categories: Counter[str] = Counter()
    for row in rows:
        counts["completed_calls"] += 1
        if row["transport_error"]:
            counts["transport_error_calls"] += 1
        else:
            counts["successful_transport_calls"] += 1
        if row["phase_turn_evidence"] and not row["transport_error"]:
            counts["driver_evaluated_calls"] += 1
        if not row["phase_turn_evidence"] and not row["transport_error"]:
            counts["successful_calls_without_phase_turn"] += 1
        if row["driver_protocol_error"]:
            counts["driver_protocol_error_calls"] += 1
            categories[row["diagnosis"]["primary_category"]] += 1
        if row["driver_protocol_error"] and row["diagnosis"]["strict_protocol_valid"]:
            counts["driver_post_parse_error_calls"] += 1
        diagnostic = row["diagnosis"]
        if not row["transport_error"] and not diagnostic["strict_protocol_valid"]:
            counts["offline_strict_protocol_invalid_calls"] += 1
        for source, target in (
            ("pure_markdown_fence", "pure_fenced_calls"),
            ("fenced_inner_json_parseable", "pure_fenced_inner_json_parseable_calls"),
            ("fenced_inner_protocol_valid", "pure_fenced_inner_protocol_valid_calls"),
            ("quote_or_escape_syntax", "quote_or_escape_syntax_calls")):
            if diagnostic[source]:
                counts[target] += 1
        if diagnostic["native_format_shapes"]:
            counts["native_format_shape_calls"] += 1
        if diagnostic["tool_format_issues"]:
            counts["tool_format_issue_calls"] += 1
        if row["actual_native_tool_events"]:
            counts["actual_native_tool_event_calls"] += 1
    keys = ("completed_calls", "successful_transport_calls", "transport_error_calls", "driver_evaluated_calls",
        "driver_protocol_error_calls", "driver_post_parse_error_calls", "successful_calls_without_phase_turn",
        "offline_strict_protocol_invalid_calls", "pure_fenced_calls", "pure_fenced_inner_json_parseable_calls",
        "pure_fenced_inner_protocol_valid_calls", "native_format_shape_calls", "quote_or_escape_syntax_calls",
        "actual_native_tool_event_calls", "tool_format_issue_calls")
    result: dict[str, Any] = {key: counts[key] for key in keys}
    denominator = counts["driver_evaluated_calls"]
    result["driver_protocol_error_rate"] = counts["driver_protocol_error_calls"] / denominator if denominator else None
    result["driver_error_categories"] = dict(sorted(categories.items()))
    return result


def build_report(root: Path) -> dict[str, Any]:
    parser, parser_info = load_frozen_parser(root)
    calls, turns, snapshot = read_inputs(root)
    rows = []
    for call_id, source in calls.items():
        record = source["record"]
        turn_evidence = turns.get(call_id, [])
        diagnostic = classify_text(record.get("text"), parser)
        driver_errors = [turn["protocol_error"] for turn in turn_evidence if turn.get("protocol_error")]
        if driver_errors and diagnostic["strict_protocol_valid"]:
            diagnostic["primary_category"] = "driver_post_parse_error"
        artifacts = record.get("raw_artifacts") or {}
        model = record.get("model_requested")
        rows.append({"call_id": call_id, "model": model, "role": record.get("role"),
            "model_family": "GPT" if str(model).startswith("gpt-") else "GLM" if str(model).startswith("glm-") else "other",
            "run_id": record.get("run_id"), "task_id": record.get("task_id"),
            "phase": turn_evidence[0]["phase"] if len(turn_evidence) == 1 else None,
            "transport_error": record.get("error") is not None,
            "transport_error_type": (record.get("error") or {}).get("type") if isinstance(record.get("error"), dict) else None,
            "actual_native_tool_events": (record.get("tools_used") or 0) > 0,
            "driver_protocol_error": bool(driver_errors), "driver_protocol_error_messages": driver_errors,
            "phase_turn_evidence": turn_evidence, "diagnosis": diagnostic,
            "evidence": {"calls_jsonl": "calls.jsonl", "completed_line": source["line"],
                "output": relative(artifacts.get("output"), root),
                "metadata": relative(artifacts.get("metadata"), root)},
        })
    model_role: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    family_role: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    run_phase: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        model_role[(str(row["model"]), str(row["role"]))].append(row)
        family_role[(row["model_family"], str(row["role"]))].append(row)
        run_phase[(str(row["run_id"]), str(row["phase"]))].append(row)
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "kind": "post_hoc_protocol_diagnostics",
        "rescored": False, "requests_retried": False, "model_calls_made": 0, "proposed_tools_executed": 0,
        "scope": "Observed robustness of this frozen JSON-text tool stack; not an isolated model coding-capability test",
        "input_snapshot": snapshot, "parser": parser_info, "overall": group_stats(rows),
        "by_model_role": [{"model": key[0], "role": key[1], **group_stats(values)} for key, values in sorted(model_role.items())],
        "by_family_role": [{"family": key[0], "role": key[1], **group_stats(values)} for key, values in sorted(family_role.items())],
        "by_run_phase": [{"run_id": key[0], "phase": key[1], "model": values[0]["model"],
                          "role": values[0]["role"], **group_stats(values)} for key, values in sorted(run_phase.items())],
        "models_without_completed_calls": [model for model in EXPECTED_MODELS if not any(row["model"] == model for row in rows)],
        "calls": rows}


def rate(value: float | None) -> str:
    return "—" if value is None else f"{100 * value:.1f}%"


def cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def markdown(report: dict[str, Any]) -> str:
    snap, overall = report["input_snapshot"], report["overall"]
    lines = ["# JSON 文本工具协议事后诊断", "", f"生成时间：{report['generated_at']}", "",
        "本报告仅检查本次冻结 JSON 文本工具栈中的格式遵循与解析结果。它没有恢复失败调用、执行模型提出的工具、重评分或调用模型，不能据此推断某个模型的编码能力高低。", "",
        f"快照包含 **{overall['completed_calls']}** 个完成调用；有阶段 turn 证据且传输成功的 **{overall['driver_evaluated_calls']}** 个调用中，driver 记录了 **{overall['driver_protocol_error_calls']}** 个协议错误（{rate(overall['driver_protocol_error_rate'])}）。", "",
        "错误率分母为已关联阶段 turn 的传输成功调用。传输失败、尚未落盘的 turn、未完成调用单列；初始执行与修复阶段的调用均计入。P/V/E 与 solo 的角色差异必须保留，因此模型家族对照也按角色列出。", "",
        "“围栏内有效”只表示删除完整的单层 Markdown 外围围栏后，离线 JSON/协议校验能通过；原始调用在执行时仍然失败，其评分与执行状态不变。“引号/转义”按 JSON 解析器错误分类，其中 delimiter 错误不能单独证明一定由引号导致。native 格式表示文本中的对象形状，实际工具执行事件另列。", "",
        "## 按模型与角色", "",
        "| 模型 | 角色 | 完成调用 | driver 已检查 | 协议错误 | 错误率 | 纯围栏 | 围栏内 JSON / 协议有效 | native / 工具字段错误 | 引号/转义语法 | 传输失败 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for row in report["by_model_role"]:
        lines.append(f"| {cell(row['model'])} | {cell(row['role'])} | {row['completed_calls']} | {row['driver_evaluated_calls']} | {row['driver_protocol_error_calls']} | {rate(row['driver_protocol_error_rate'])} | {row['pure_fenced_calls']} | {row['pure_fenced_inner_json_parseable_calls']} / {row['pure_fenced_inner_protocol_valid_calls']} | {row['native_format_shape_calls']} / {row['tool_format_issue_calls']} | {row['quote_or_escape_syntax_calls']} | {row['transport_error_calls']} |")
    lines += ["", "## GLM 与 GPT 的同角色对照", "",
        "| 家族 | 角色 | driver 已检查 | 协议错误 | 错误率 | 离线严格协议无效 |",
        "|---|---|---:|---:|---:|---:|"]
    for row in report["by_family_role"]:
        lines.append(f"| {row['family']} | {cell(row['role'])} | {row['driver_evaluated_calls']} | {row['driver_protocol_error_calls']} | {rate(row['driver_protocol_error_rate'])} | {row['offline_strict_protocol_invalid_calls']} |")
    lines += ["", "这是该快照的描述性对照；不同任务、消息历史、角色调用次数以及内置 CLI 上下文均可能不同。表中不提供编码能力排名或因果归因。", "",
        "## 按运行与阶段", "", "| 运行 | 阶段 | 模型 | driver 已检查 | 协议错误 | 错误率 |",
        "|---|---|---|---:|---:|---:|"]
    for row in report["by_run_phase"]:
        lines.append(f"| {cell(row['run_id'])} | {cell(row['phase'])} | {cell(row['model'])} | {row['driver_evaluated_calls']} | {row['driver_protocol_error_calls']} | {rate(row['driver_protocol_error_rate'])} |")
    lines += ["", "## 错误调用证据", "", "只列调用 ID、错误类别、解析位置和文件路径；完整模型输出保留在原始 artifact 中。", "",
        "| Call ID | 模型 / 角色 | 类别 | 短说明 | 证据 |", "|---|---|---|---|---|"]
    for row in report["calls"]:
        diag = row["diagnosis"]
        if not (row["driver_protocol_error"] or row["transport_error"] or not diag["strict_protocol_valid"]):
            continue
        detail = diag.get("inner_syntax_error") or diag.get("syntax_error")
        note = (f"{detail['message']} @ {detail['line']}:{detail['column']}" if detail else
                diag.get("schema_error") or row.get("transport_error_type") or "")
        if diag["pure_markdown_fence"]:
            note = f"fenced JSON={diag['fenced_inner_json_parseable']}; protocol={diag['fenced_inner_protocol_valid']}; " + note
        if row["driver_protocol_error_messages"] and not note:
            note = row["driver_protocol_error_messages"][0]
        if diag["tool_format_issues"]:
            note += "; " + ", ".join(diag["tool_format_issues"])
        evidence = row["phase_turn_evidence"][0]["path"] if row["phase_turn_evidence"] else row["evidence"]["output"]
        evidence = (evidence or "calls.jsonl") + f"; calls.jsonl:{row['evidence']['completed_line']}"
        category = "transport_error / " + diag["primary_category"] if row["transport_error"] else diag["primary_category"]
        lines.append(f"| {row['call_id']} | {cell(row['model'])} / {cell(row['role'])} | {category} | {cell(note[:220])} | {cell(evidence)} |")
    lines += ["", "## 证据完整性", "",
        f"- 未完成调用：{len(snap['started_without_completed'])}；成功但未关联 turn：{overall['successful_calls_without_phase_turn']}。",
        f"- 重复 completed 行：{snap['duplicate_completed_records']}；输入读取警告：{len(snap['warnings'])}。",
        f"- driver 解析后错误：{overall['driver_post_parse_error_calls']}；实际 native 工具事件调用：{overall['actual_native_tool_event_calls']}。",
        f"- 完成阶段的 protocol_errors 与逐 turn 核对不一致：{sum(not row['counts_match'] for row in snap['phase_summary_checks'])}。",
        f"- 解析器与冻结 manifest 一致：{report['parser']['matches_frozen_manifest']}。",
        f"- calls.jsonl 快照 SHA256：`{snap['calls_snapshot_sha256']}`。", "",
        "每个调用的细分标记、原始证据路径、行号及聚合分母见 `protocol_diagnostics.json`。"]
    return "\n".join(lines) + "\n"


def self_test(root: Path) -> None:
    parser, _ = load_frozen_parser(root)
    valid = '{"type":"tool_calls","calls":[{"name":"run","arguments":{"cmd":"echo OK"}}]}'
    assert classify_text(valid, parser)["strict_protocol_valid"]
    fenced = classify_text("```json\n" + valid + "\n```", parser)
    assert fenced["pure_markdown_fence"] and fenced["fenced_inner_protocol_valid"]
    assert not fenced["strict_protocol_valid"]
    assert classify_text('prefix\n```json\n' + valid + '\n```', parser)["fenced_inner_protocol_valid"] is None
    native = classify_text('{"tool_calls":[{"function":{"name":"run","arguments":"{}"}}]}', parser)
    assert native["primary_category"] == "native_tool_json_format"
    bad_escape = classify_text(r'{"type":"final","content":"\q"}', parser)
    assert bad_escape["primary_category"] == "json_invalid_escape"
    bad_quotes = classify_text('{"type":"final","content":"print("x")"}', parser)
    assert bad_quotes["quote_or_escape_syntax"]
    assert classify_text('{"type":"tool_calls","calls":[]}', parser)["primary_category"] == "wrong_protocol_schema"
    assert "argument_fields_at_call_top_level" in classify_text('{"type":"tool_calls","calls":[{"name":"write","path":"x","content":"x"}]}', parser)["tool_format_issues"]
    assert classify_text('{"type":"final","content":"DONE"}', parser)["strict_protocol_valid"]
    print(json.dumps({"self_tests_passed": 9, "model_calls": 0, "tools_executed": 0}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--check-only", action="store_true", help="Read and classify in memory; do not write any report")
    parser.add_argument("--self-test", action="store_true", help="Run only offline synthetic parser checks, writing no files")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.self_test:
        self_test(root)
        return
    report = build_report(root)
    if not args.check_only:
        output = (args.output_dir or root).resolve()
        output.mkdir(parents=True, exist_ok=True)
        (output / "protocol_diagnostics.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (output / "PROTOCOL_DIAGNOSTICS.md").write_text(markdown(report), encoding="utf-8")
    print(json.dumps({"wrote_reports": not args.check_only, "overall": report["overall"],
        "by_model_role": report["by_model_role"], "warnings": report["input_snapshot"]["warnings"]}, ensure_ascii=True))


if __name__ == "__main__":
    main()
