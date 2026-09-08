"""Publish a privacy-minimized, byte-bound pelican snapshot without running DPH.

Python standard library only. An existing output directory is never overwritten.
Source manifests are read once into memory; their embedded result hashes bind the
artifacts and native exports to that cutoff even while later experiments continue.
No browser, generated program, model API, or experiment scheduler is invoked.
"""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import html
import io
import json
from pathlib import Path
import re
from datetime import datetime, timezone
from urllib.parse import unquote
import zipfile


MODELS = {"deepseek-v4-pro", "deepseek-v4-flash", "glm-5.3", "glm-5.3-flash"}
GROUPS = {
    "original32": "原始 32 组 · 四模型历史对照",
    "budget48": "预算主矩阵 48 组 · 截止快照",
    "historical15": "旧版 0.7.0 · 独立历史 15 组",
    "save_retry1": "保存补跑 · 单次 Pro 例外",
}
PRIVACY = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]|(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,})|Bearer\s+[A-Za-z0-9._-]{20,}|93711", re.I)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def records(document):
    rows = document["runs"]
    return list(rows.values()) if isinstance(rows, dict) else rows


def safe_model(value):
    return value if value in MODELS else None


class Publisher:
    def __init__(self, source_root: Path, output: Path):
        self.root, self.output = source_root, output
        self.sources, self.files, self.transforms = {}, {}, []
        self.started = now()
        self.notes = []

    def source(self, path, logical_id, expected=None):
        data = Path(path).read_bytes()
        actual = digest(data)
        if expected and actual != expected:
            raise ValueError(f"Frozen source hash mismatch: {logical_id}")
        old = self.sources.get(logical_id)
        if old and old["sha256"] != actual:
            raise ValueError(f"Source changed during capture: {logical_id}")
        self.sources[logical_id] = {"source_id": logical_id, "sha256": actual, "bytes": len(data)}
        return data

    def document(self, relative, logical_id):
        return json.loads(self.source(self.root / relative, logical_id))

    def add(self, relative, data, source_id=None, transform="identity-bytes"):
        relative = Path(relative).as_posix()
        if relative.startswith("/") or ".." in Path(relative).parts:
            raise ValueError("Unsafe publication path")
        if relative in self.files:
            raise ValueError(f"Duplicate output: {relative}")
        self.files[relative] = data
        self.transforms.append({"path": relative, "sha256": digest(data), "bytes": len(data), "source_id": source_id, "transform": transform})

    def native_routes(self, run, cohort):
        result = run.get("result") or {}
        if not result.get("ended") or not result.get("export_path"):
            return {}, "not_observed_at_cutoff"
        sid_to_role = {s.get("session_id"): s.get("role", "unknown") for s in result.get("sessions", [])}
        raw = self.source(result["export_path"], f"{cohort}/{run['run_id']}/native-export", result.get("export_sha256"))
        found = collections.defaultdict(lambda: {"request_models": set(), "response_models": set(), "efforts": set()})
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for entry in archive.namelist():
                if not entry.endswith(".jsonl"):
                    continue
                events = [json.loads(line) for line in archive.read(entry).decode("utf-8").splitlines() if line.strip()]
                role = sid_to_role.get(events[0].get("id"), "lead" if entry == "session.jsonl" else "unclassified_child")
                item = found[role]
                for event in events:
                    data = event.get("data", {})
                    if event.get("type") == "request/header":
                        config = data.get("header", {}).get("config", {})
                        model = safe_model(config.get("model"))
                        if model:
                            item["request_models"].add(model)
                        effort = config.get("reasoning_effort", config.get("reasoningEffort"))
                        if effort in {"none", "low", "medium", "high", "xhigh", "max"}:
                            item["efforts"].add(effort)
                    elif event.get("type") == "assistant/message":
                        model = safe_model(data.get("message", {}).get("source", {}).get("model"))
                        if model:
                            item["response_models"].add(model)
        return {role: {key: sorted(values) for key, values in item.items()} for role, item in found.items()}, "native_request_header_and_response_source"

    def artifact(self, entry, row, ordinal, confidence="confirmed_final"):
        path = entry.get("path", entry.get("snapshot"))
        expected = entry.get("sha256")
        if not path or not expected:
            return {"status": "unconfirmed", "reason": "missing_hash_binding"}
        source_id = f"{row['cohort']}/{row['run_id']}/artifact-{ordinal}"
        try:
            raw = self.source(path, source_id, expected)
        except (OSError, ValueError):
            return {"status": "withheld", "reason": "source_missing_or_hash_changed", "sha256": expected}
        if PRIVACY.search(raw.decode("utf-8", errors="replace")):
            return {"status": "withheld", "reason": "personal_path_or_credential_pattern", "sha256": expected}
        target = f"artifacts/{row['cohort']}/{row['run_id']}/{confidence}-{ordinal}.html"
        self.add(target, raw, source_id)
        return {"status": confidence, "path": target, "sha256": expected, "bytes": len(raw)}

    def public_row(self, run, cohort, route_audit, save_audit, supplement=None):
        result = run.get("result") or {}
        state = run.get("state", "unknown")
        row = {
            "cohort": cohort, "run_id": run["run_id"], "model": safe_model(run.get("model")),
            "mode": run.get("preset", run.get("mode")), "arm": run.get("arm"),
            "state": state, "plugin_version": run.get("plugin_version", "historical_unspecified"),
            "team_requested": bool(run.get("team_enabled", run.get("arm") in {"team", "team_cm"})),
            "cm_requested": bool(run.get("cm_enabled", run.get("arm") in {"cm", "team_cm"})),
            "team_invoked": result.get("actual_team_invoked"), "team_call_count": result.get("actual_team_call_count"),
            "cm_calls": result.get("cm_calls"), "cm_adopted": result.get("cm_adopted"),
            "worker_budget_mode": (run.get("worker_budget") or {}).get("mode"),
            "elapsed_seconds": result.get("elapsed_seconds"),
            "fee_usd": result.get("fee_usd"), "fee_status": result.get("fee_status", "unknown_not_zero"),
            "route_issue": "not_independently_classified", "artifacts": [],
        }
        reason = result.get("turn_end_reason", run.get("turn_end_reason")) or {}
        row["terminal_kind"] = reason.get("kind")
        code = (reason.get("error") or {}).get("code")
        row["terminal_error_code"] = code if code in {"RATE_LIMIT", "BUDGET_EXHAUSTED", "WORKER_TOKEN_RESERVATION_DENIED", "WORKER_CALL_LIMIT"} else None
        routes, evidence = self.native_routes(run, cohort)
        row["observed_routes"], row["route_evidence"] = routes, evidence
        cm = {safe_model(c.get("profile", {}).get("model")) for c in result.get("cm_records", [])}
        row["observed_cm_models"] = sorted(cm - {None})
        old = route_audit.get(run["run_id"])
        if cohort == "budget48" and old and old.get("root_session_id") == run.get("session_id"):
            row["route_issue"] = "confirmed_implementer_wrong_pro_high" if old.get("confirmed_wrong_implementer_sessions") else "team_not_invoked" if old.get("team_enabled") and not old.get("team_call_count") else "no_implementer_request_unknown" if old.get("team_enabled") and not routes.get("implementer", {}).get("request_models") else "no_confirmed_model_deviation"
        elif cohort == "budget48" and run.get("plugin_version") == "0.7.2":
            lead = routes.get("lead", {}).get("request_models", [])
            impl = routes.get("implementer", {}).get("request_models", [])
            row["route_issue"] = "lead_request_matches" if lead == [row["model"]] else "not_observed_at_cutoff"
            if impl:
                row["route_issue"] = "lead_and_implementer_request_match" if impl == [row["model"]] and lead == [row["model"]] else "observed_model_deviation"
        entries = result.get("artifacts", [])
        verified = save_audit.get((cohort, run["run_id"]))
        if supplement:
            entries = supplement["artifacts"]
            row["collection_note"] = "Final successful edit-chain supplement replaces the initial snapshot for display; original evidence is preserved."
        ended = state in {"finished", "reused"} and bool(result.get("ended"))
        for index, entry in enumerate(entries):
            final = entry.get("final_state_confirmed") is True
            if cohort == "original32" and verified:
                final = any(a.get("sha256") == entry.get("sha256") and a.get("hash_match") for a in verified.get("artifacts", []))
            if final and ended:
                row["artifacts"].append(self.artifact(entry, row, index))
        row["save_status"] = "confirmed_final" if any(a.get("status") == "confirmed_final" for a in row["artifacts"]) else "withheld" if row["artifacts"] else "not_finished" if not ended else "no_confirmed_final_html"
        row.update(self.metrics(result))
        return row

    def build(self):
        if self.output.exists():
            raise FileExistsError("Use an independent new output directory")
        p = "dph-pelican-20260908"
        b = "dph-budget-comparison-20260908"
        original = self.document(f"{p}/manifest.json", "original32/manifest")
        main = self.document(f"{b}/manifest.json", "budget48/manifest")
        cutoff = now()
        historical = self.document(f"{b}/ARTIFACTS_CURRENT.json", "historical15/snapshot")
        retry = self.document(f"{b}/SAVE_FAILURE_RETRY.json", "save_retry1/record")
        route = self.document(f"{b}/evidence/heartbeat-20260908T0306/ROUTE_AUDIT_COMPLETE.json", "budget39/route-audit")
        saved = self.document(f"{b}/evidence/heartbeat-20260908T0306/SAVE_STATUS_AUDIT.json", "saving/audit")
        supplemental = self.document(f"{b}/evidence/auto__v4flash__cordis__team/artifact-final-reconstruction.json", "budget39/final-edit-supplement")
        route_lookup = {r["run_id"]: r for r in route["groups"]}
        cohort_map = {"pelican32": "original32", "budget39": "budget48"}
        save_lookup = {(cohort_map.get(r["cohort"], r["cohort"]), r["run_id"]): r for r in saved["attempts"]}
        selected = [r for r in records(main) if not r.get("dispatch_excluded")]
        if len(records(original)) != 32 or len(selected) != 48:
            raise ValueError("Unexpected denominators; refuse to silently change scope")
        rows = [self.public_row(r, "original32", route_lookup, save_lookup) for r in records(original)]
        rows += [self.public_row(r, "budget48", route_lookup, save_lookup, supplemental if r["run_id"] == supplemental["run_id"] and r.get("session_id") == supplemental["session_id"] else None) for r in selected]
        for run in records(historical):
            row = {"cohort": "historical15", "run_id": run["run_id"], "model": safe_model(run.get("model")), "mode": run.get("mode"), "arm": run.get("arm"), "state": "historical_ended", "plugin_version": "0.7.0", "fee_usd": None, "fee_status": "unknown_not_zero", "route_issue": "historical_not_reclassified", "artifacts": []}
            for index, entry in enumerate(run.get("artifacts", [])):
                if not entry.get("write_verified"):
                    continue
                row["artifacts"].append(self.artifact(entry, row, index, "confirmed_final" if entry.get("is_final") else "intermediate_only"))
            row["save_status"] = "confirmed_final" if any(a["status"] == "confirmed_final" for a in row["artifacts"]) else "intermediate_only" if row["artifacts"] else "save_failed_replaced_separately"
            row.update(self.metrics({}))
            rows.append(row)
        rows.append(self.public_row(retry, "save_retry1", route_lookup, save_lookup))
        frames = self.document(f"{p}/visualization/final/frames.json", "original32/existing-frame-provenance")
        frame_map = {r["run_id"]: r for r in frames["frames"]}
        for row in rows:
            if row["cohort"] != "original32" or not row["artifacts"]:
                continue
            frame = frame_map.get(row["run_id"], {})
            if frame.get("source_sha256") != row["artifacts"][0].get("sha256") or frame.get("status") not in {"cached", "captured", "success"}:
                continue
            raw = self.source(frame["output_path"], f"original32/{row['run_id']}/existing-frame", frame["screenshot_sha256"])
            target = f"previews/{row['run_id']}.png"
            self.add(target, raw, f"original32/{row['run_id']}/existing-frame")
            row["preview"] = target
        counts = {group: {"cells": sum(r["cohort"] == group for r in rows), "states": dict(collections.Counter(r["state"] for r in rows if r["cohort"] == group)), "saved_final": sum(r["cohort"] == group and r["save_status"] == "confirmed_final" for r in rows), "intermediate_only": sum(r["cohort"] == group and r["save_status"] == "intermediate_only" for r in rows)} for group in GROUPS}
        wrong_count = sum(r["cohort"] == "budget48" and r["route_issue"] == "confirmed_implementer_wrong_pro_high" for r in rows)
        if wrong_count != 17:
            raise ValueError("The frozen 17-cell deviation set changed; publication needs explicit review")
        data = {"schema_version": 1, "cutoff_utc": cutoff, "manifest_capture_started_utc": self.started, "snapshot_semantics": "Rows reflect manifests captured at this cutoff, not a live dashboard. Finished is a collection state; save status, native termination and routing are separate.", "prompt": "创建一个HTML，内容是SVG绘制一个鹈鹕骑自行车的2D动画，放到本机dev目录，你不需要任何测试", "counts": counts, "known_wrong_route_cells": wrong_count, "usage_policy": "Usage totals are observed lower bounds when incomplete. Completed model replies are reported separately from unknown retry attempts. No authentication tokens are exported; unknown fees remain null.", "rows": rows}
        self.add("results.json", (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode(), transform="allowlisted-public-fields; no raw sessions or private paths")
        columns = ["cohort", "run_id", "model", "mode", "arm", "state", "plugin_version", "save_status", "team_requested", "team_invoked", "team_call_count", "cm_requested", "cm_calls", "cm_adopted", "worker_budget_mode", "elapsed_seconds", "fee_usd", "fee_status", "route_issue", "observed_total_tokens", "ordinary_observed_tokens", "cm_observed_tokens", "ordinary_calls_observed", "usage_complete", "usage_unknown_messages", "retry_usage_unknown_attempts", "cm_usage_unknown_calls", "lead_allocations", "auto_allocations", "actual_worker_profiles", "artifact_paths"]
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            item = {key: row.get(key) for key in columns}
            for key in ["lead_allocations", "auto_allocations", "actual_worker_profiles"]:
                item[key] = json.dumps(row.get(key), ensure_ascii=False) if row.get(key) is not None else None
            item["artifact_paths"] = " | ".join(a["path"] for a in row["artifacts"] if a.get("path"))
            writer.writerow(item)
        self.add("results.csv", stream.getvalue().encode("utf-8-sig"), transform="allowlisted-public-fields; blank means unknown")
        self.add("protocol.json", (json.dumps(self.protocol(data), ensure_ascii=False, indent=2) + "\n").encode(), transform="requested protocol allowlist from captured manifests; not actual exposure")
        self.add("README.md", self.readme(data).encode(), transform="public explanatory text")
        self.add("index.html", self.gallery(data).encode(), transform="escaped metadata and relative navigation; existing static previews only")
        self.validate_memory()
        manifest = {"schema_version": 1, "cutoff_utc": cutoff, "source_identifiers": "Logical source IDs, not local filesystem paths. Raw private manifests and native ZIPs are not published.", "input_sources": sorted(self.sources.values(), key=lambda s: s["source_id"]), "exports": self.transforms, "artifacts_transformed": False, "models_called": False, "dph_accessed": False, "generated_artifacts_executed": False, "existing_previews_reused": sum("preview" in r for r in rows), "new_screenshots_created": 0, "validation": {"artifact_identity_hashes": "passed", "relative_links": "passed", "privacy_pattern_scan": "passed"}}
        self.files["publication-manifest.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
        if self.output.exists():
            raise FileExistsError("Use an independent new output directory; existing releases are never overwritten")
        self.output.mkdir(parents=True)
        for relative, raw in self.files.items():
            target = self.output / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            if digest(target.read_bytes()) != digest(raw):
                raise OSError("Publication write verification failed")
        return {"cutoff_utc": cutoff, "counts": counts, "files": len(self.files), "known_wrong_route_cells": wrong_count, "publication_manifest_sha256": digest(self.files["publication-manifest.json"])}

    def validate_memory(self):
        for path, raw in self.files.items():
            if not path.endswith((".html", ".json", ".csv", ".md")):
                continue
            text = raw.decode("utf-8-sig")
            if PRIVACY.search(text):
                raise ValueError(f"Private-data pattern in public output: {path}")
            if path == "index.html":
                for link in re.findall(r'(?:href|src)="([^"]+)"', text):
                    if link.startswith("#") or ":" in link:
                        continue
                    if unquote(html.unescape(link)) not in self.files and link != "publication-manifest.json":
                        raise ValueError(f"Broken relative gallery link: {link}")

    @staticmethod
    def metrics(result):
        sessions = result.get("sessions")
        replies = [s.get("assistant_messages") for s in sessions] if isinstance(sessions, list) else None
        ordinary_calls = sum(replies) if replies is not None and all(isinstance(n, int) for n in replies) else None
        allocations = [] if isinstance(result.get("lead_actual_allocations"), list) else None
        for event in result.get("lead_actual_allocations", []):
            d = event.get("data", {})
            allocations.append({"role": d.get("label") if d.get("label") in {"implementer", "tester", "reviewer"} else "unclassified", "token_limit": d.get("tokenLimit"), "call_limit": d.get("callLimit"), "mode": (d.get("profile") or {}).get("mode"), "decision_source": "configured_manual_limit" if (d.get("profile") or {}).get("mode") == "manual" else "lead_tool_call" if d.get("decided_by") == "current_lead_tool_call" else "recorded_allocation"})
        profiles = [] if isinstance(result.get("role_states"), list) else None
        for role in result.get("role_states", []):
            p = role.get("profile") or {}
            profiles.append({"role": role.get("role") if role.get("role") in {"implementer", "tester", "reviewer"} else "unclassified", "mode": p.get("mode"), "token_limit": p.get("tokenLimit"), "call_limit": p.get("callLimit"), "budget_applied": role.get("actual_worker_budget_applied")})
        return {"observed_total_tokens": result.get("observed_total_tokens"), "ordinary_observed_tokens": (result.get("agent_usage") or {}).get("observed_total_tokens"), "cm_observed_tokens": (result.get("cm_usage") or {}).get("observed_total_tokens"), "ordinary_calls_observed": ordinary_calls, "usage_complete": result.get("usage_complete"), "usage_unknown_messages": result.get("usage_unknown_messages"), "retry_usage_unknown_attempts": result.get("retry_usage_unknown_attempts"), "cm_usage_unknown_calls": result.get("cm_usage_unknown_calls"), "lead_allocations": allocations, "auto_allocations": [a for a in allocations if a.get("mode") == "auto"] if allocations is not None else None, "actual_worker_profiles": profiles}

    def protocol(self, data):
        return {
            "cutoff_utc": data["cutoff_utc"],
            "prompt": data["prompt"],
            "models": sorted(MODELS),
            "modes": ["standard", "cordis"],
            "arms": {"baseline": {"team": False, "cm": False}, "cm": {"team": False, "cm": True}, "team": {"team": True, "cm": False}, "team_cm": {"team": True, "cm": True}},
            "requested_role_policy": {"lead": "selected conversation model", "implementer": "follow conversation model", "tester": "glm-5.3-flash", "reviewer": "Lead", "cm": "glm-5.3-flash", "deepseek_reasoning_effort": "max"},
            "budget_protocol": {"scope": "each child worker separately; no Lead/team aggregate cap", "original32": "no child lifetime token/call caps; historical reference only", "manual_2x_reference": {"per_worker_token_limit": 1200000, "per_worker_call_limit": 56, "reference": "2x the new manual-form 600000/28 reference; not 2x an original32 cap"}, "auto": "Lead reads the task and chooses separate worker limits before delegation"},
            "interpretation": "Requested policy, not proof of actual exposure. results.json records request/response models; the 17 confirmed old implementer deviations remain marked. The full task prompt is unchanged between cells.",
        }

    def readme(self, data):
        lines = ["# DPH 鹈鹕动画实验公开快照", "", f"截止时间：**{data['cutoff_utc']}**。这是一份静态发布，后续运行不会自动改变它。", "", "[打开离线画廊](index.html) · [逐组 CSV](results.csv) · [结构化 JSON](results.json) · [源与发布哈希](publication-manifest.json)", "", "同一提示词：", "", "> " + data["prompt"], "", "## 分母与完成边界", "", "| 批次 | 组数 | 已确认最终 HTML | 仅中间稿 | 状态 |", "|---|---:|---:|---:|---|" ]
        for key, title in GROUPS.items():
            c = data["counts"][key]
            lines.append(f"| {title} | {c['cells']} | {c['saved_final']} | {c['intermediate_only']} | {json.dumps(c['states'], ensure_ascii=False)} |")
        lines += ["", "原32组包含 V4 Pro 历史。预算主矩阵固定48组；待派发、运行中与API终止格都保留，不冒充全部完成。旧15组与单次Pro保存补跑单独展示，不加入主48分母。旧保存失败保留记录，成功补跑不回填覆盖旧尝试。", "", "## 路由与机制证据", "", "主矩阵原0.7.1批次有 **17格确认实现者错误路由到 DeepSeek V4 Pro / high**，均已逐格标记，不能用于声称预期模型配置的团队效果。新0.7.2格的已观察路由来自原生 request/header 与模型回复 source；未派发或无请求的角色保留未知。原始32组展示实际观察模型，不把它们自动判定为已完成独立路由审计。", "", "开关表示请求启用；team_invoked、team_call_count、cm_calls、cm_adopted 才描述实际调用与采用。没有调用不等于机制已充分接受检验。未知费用保留 null / CSV 空格，不等于零；不根据订阅额度推算费用。", "", "## 保存与作品", "", "HTML按源字节复制，公开文件SHA与原始保存记录一致。auto V4 Flash 创造模式团队格采用已验证的最终 write/edit 补充快照，旧初版证据仍保留。旧15里仅有成功中间稿、未确认最终版本的作品明确标注为中间稿。", "", "画廊只加载已存在的静态缩略图并提供HTML链接；本次发布没有调用模型、启动DPH或运行动画。缩略图是历史固定时刻画面，只有其源HTML哈希与公开HTML相同才复用。截图、成功保存、官方评测和视觉质量是不同概念；这里不提供质量排名。", "", "下载整个目录后打开 index.html 即可浏览；点击作品会由浏览器运行动画。保持 artifacts 与 previews 相对目录。文件可能使用作者原本引用的外部资源，本次发布不替换或补齐它们。", "", "## 可追溯与隐私", "", "publication-manifest.json 记录每个输入源的逻辑标识和SHA、每个导出文件的SHA及变换。仅公开必要字段，不发布密钥、认证token、宿主profile、会话原文或个人本机路径。带个人路径或凭据特征的HTML会被留空，不修改内容后冒充原作。", "", "生成器：仓库 scripts/publish_pelican.py。必须指定独立的新输出目录；不会覆盖现有发布或修改实验源数据。", ""]
        lines += ["## 历史例图：原32组 · 标准模式 · 基线", "", "以下为同一固定时刻的历史静态画面，只作作品入口，不是质量排名。", "", "| V4 Pro | V4 Flash | GLM 5.3 | GLM 5.3 Flash |", "|---|---|---|---|", "| " + " | ".join(f"[![{model}](previews/{key}__standard__baseline.png)](artifacts/original32/{key}__standard__baseline/confirmed_final-0.html)" for model,key in [("V4 Pro","v4pro"),("V4 Flash","v4flash"),("GLM 5.3","glm53"),("GLM 5.3 Flash","glm53flash")]) + " |", "", "## 用量口径", "", "observed_total_tokens 直接保留collector已观测值；ordinary_observed_tokens 与 cm_observed_tokens 分列。usage_complete=false 时，它们是观测下界，不补估缺失用量。ordinary_calls_observed 是原生已完成模型回复条数，不包含无法确认的内部重试；未知消息/重试/CM用量另外保留。Auto实际额度只导出角色、token/调用上限与决策来源，不公开理由或任务全文。无来源字段为null/CSV空白。", "", "## 冻结配置协议", "", "[查看完整公开协议](protocol.json)。实现者请求跟随对话模型；测试者与CM请求使用 GLM 5.3 Flash；Reviewer由Lead承担。DeepSeek推理强度请求为max。实际路由与开关曝光以逐组证据为准。", "", "原32组没有子worker终身token/调用限额。新手动2倍组对每个子worker分别设120万token/56次调用；这是新表单参考60万/28的两倍，不能称作原32组预算的两倍。Auto由Lead读题后分别分配子worker额度；不限制Lead或整个团队。", "", "[发布脚本](../../../scripts/publish_pelican.py) · [公开数据](results.json)", ""]
        return "\n".join(lines)

    def gallery(self, data):
        e = html.escape
        sections = []
        for key, title in GROUPS.items():
            cards = []
            for row in [r for r in data["rows"] if r["cohort"] == key]:
                preview = f'<img loading="lazy" src="{e(row["preview"])}" alt="历史画面：{e(row["model"] or "unknown")}">' if row.get("preview") else '<div class="placeholder">' + ("已保存 · 打开查看作品" if row["save_status"] == "confirmed_final" else "中间稿 · 最终版本未确认" if row["save_status"] == "intermediate_only" else "暂无已确认最终作品") + '</div>'
                warning = '<p class="warning">配置偏差：实现者实际为 V4 Pro / high</p>' if row["route_issue"] == "confirmed_implementer_wrong_pro_high" else ''
                links = ''.join(f'<a class="open" href="{e(a["path"])}" target="_blank" rel="noopener">{"打开中间稿" if a["status"] == "intermediate_only" else "打开动画"} ↗</a>' for a in row["artifacts"] if a.get("path"))
                observed = row.get("observed_routes", {})
                route_text = ' · '.join(f'{role}: {", ".join(info.get("request_models", [])) or "无请求证据"}' for role, info in observed.items())
                cards.append(f'<article class="card">{preview}<div class="body"><h3>{e(row["model"] or "unknown")}</h3><p class="label">{e(str(row["mode"]))} · {e(str(row["arm"]))}</p><p class="badge">{e(row["state"])} · {e(row["save_status"])}</p>{warning}<p class="micro">{e(route_text)}</p>{links}<details><summary>实验标识与机制记录</summary><p>{e(row["run_id"])}</p><p>Team调用：{e(str(row.get("team_call_count", "未知")))} · CM采用/调用：{e(str(row.get("cm_adopted", "未知")))}/{e(str(row.get("cm_calls", "未知")))}</p><p>费用：{e(str(row.get("fee_usd"))) if row.get("fee_usd") is not None else "未知"}</p></details></div></article>')
            c = data["counts"][key]
            sections.append(f'<section id="{key}"><div class="section-head"><h2>{e(title)}</h2><p>{c["saved_final"]} / {c["cells"]} 组有已确认最终HTML · 中间稿 {c["intermediate_only"]}</p></div><div class="grid">{"".join(cards)}</div></section>')
        return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>DPH · 鹈鹕动画实验画廊</title><style>body{margin:0;background:#f6f7f9;color:#172334;font:15px/1.6 system-ui,sans-serif}main{max-width:1500px;margin:auto;padding:46px 28px}.eyebrow{letter-spacing:.14em;font-size:12px;color:#55717b}h1{font-size:clamp(30px,4vw,52px);line-height:1.15;margin:12px 0 18px}header>p{max-width:920px;color:#596875}a{color:#0c6878}nav{display:flex;gap:12px;flex-wrap:wrap;margin:28px 0}nav a{padding:8px 14px;border:1px solid #d6e1e5;border-radius:20px;background:white;text-decoration:none}.notice{padding:18px 22px;background:#fff7e7;border:1px solid #eedbb6;border-radius:12px;max-width:1080px}.section-head{display:flex;justify-content:space-between;align-items:baseline;gap:16px;margin:48px 0 16px}.section-head p{color:#607080}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:18px}.card{background:white;border:1px solid #dfe5e9;border-radius:14px;overflow:hidden}.card img,.placeholder{width:100%;aspect-ratio:1.6;object-fit:contain;background:#eef3f4}.placeholder{display:grid;place-items:center;color:#677981;font-size:13px}.body{padding:18px}h3{font-size:17px;margin:0}.label{margin:4px 0 8px;color:#5b6b77}.badge{font-size:11px;overflow-wrap:anywhere}.warning{font-size:12px;padding:7px 9px;border-radius:6px;color:#8d4d17;background:#fff0db}.micro,details{font-size:11px;color:#61717b;overflow-wrap:anywhere}.open{display:inline-block;margin:9px 0 12px;text-decoration:none;font-weight:600}details summary{cursor:pointer}footer{margin:44px 0;color:#697a86;font-size:12px}@media(max-width:1120px){.grid{grid-template-columns:repeat(3,minmax(0,1fr))}}@media(max-width:820px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.section-head{display:block}}@media(max-width:520px){main{padding:28px 16px}.grid{grid-template-columns:1fr}}</style></head><body><main><header><div class="eyebrow">DPSWARM / DPH EXPERIMENTS</div><h1>一只鹈鹕，四种模型。<br>把实验作品和边界一起公开。</h1><p>同一任务，不同模式、团队开关与CM设置。这里保留原作、完成状态和实际路由证据，不根据一张截图评判机制胜负。</p><p>静态快照截止：' + e(data['cutoff_utc']) + '</p><nav>' + ''.join(f'<a href="#{k}">{e(v)}</a>' for k,v in GROUPS.items()) + '<a href="README.md">阅读说明</a><a href="results.csv">下载CSV</a><a href="results.json">JSON</a><a href="publication-manifest.json">哈希清单</a></nav><div class="notice">主矩阵旧批次有17格实现者模型配置偏差，已逐格标记。运行中与待派发格保留；旧15组和单次保存补跑采用独立分母。未知费用不计为零。</div></header>' + ''.join(sections) + '<footer>本次发布没有运行动画或创建新截图。预览来自已存在、与HTML哈希绑定的历史画面。点击“打开动画”后才由浏览器打开原作。</footer></main></body></html>'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    repository = Path(__file__).resolve().parents[1]
    parser.add_argument("--source-root", type=Path, default=repository / "tihu test")
    parser.add_argument("--output", type=Path, required=True, help="New, independent directory; must not exist")
    arguments = parser.parse_args()
    result = Publisher(arguments.source_root.resolve(), arguments.output.resolve()).build()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
