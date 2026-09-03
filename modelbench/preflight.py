#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""preflight.py — GLM / DeepSeek 模型连通性与模型名有效性预检(标准库,零依赖)。

对每个候选模型 id 发一次最小 completion(max_tokens=8,"回复 ok")验证连通性与
模型名有效性;GLM 侧额外发一次带内置 web_search 工具的调用,验证 tools 格式。
结果打印成表并写入 modelbench/results/preflight.json。

环境变量:
  GLM_API_KEY        必填(跑 GLM 侧检查)
  GLM_BASE_URL       可选,默认 https://open.bigmodel.cn/api/paas/v4
                     (Coding Plan 编程通道第三方口径为
                      https://open.bigmodel.cn/api/coding/paas/v4,官方未文档化,需实测)
  DEEPSEEK_API_KEY   必填(跑 DeepSeek 侧检查)
  DEEPSEEK_BASE_URL  可选,默认 https://api.deepseek.com
  GLM_MODELS         可选,逗号分隔,覆盖默认 GLM 候选列表
  DEEPSEEK_MODELS    可选,逗号分隔,覆盖默认 DeepSeek 候选列表
  GLM_WEBSEARCH_MODEL 可选,默认 glm-4.7-flash(免费模型,搜索 search_std ¥0.01/次)

默认候选包含已下线/已停用的旧名(glm-4.5-flash、deepseek-chat、deepseek-reasoner),
预期它们失败 —— 这正是预检要证明的事实。背景结论见 modelbench/MODELS.md(核实日期 2026-09-03)。

用法:
  python preflight.py            # 有哪边的 key 就跑哪边;都没有则打印缺哪些变量并退出
退出码: 0=全部通过; 1=有失败项; 2=未配置任何 key。
"""

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
import keyconfig  # noqa: E402  env > keys.local.json 两级读取

RESULTS_PATH = os.path.join(SCRIPT_DIR, "results", "preflight.json")

GLM_BASE_URL_DEFAULT = "https://open.bigmodel.cn/api/paas/v4"
GLM_CODING_BASE_URL = "https://open.bigmodel.cn/api/coding/paas/v4"  # Coding Plan 通道(第三方口径,本脚本实测)
DEEPSEEK_BASE_URL_DEFAULT = "https://api.deepseek.com"

PROMPT = "回复 ok"
MAX_TOKENS = 8
TIMEOUT_SECONDS = 90

# 默认候选:重点验证实验计划用到的名字;旧名预期失败(见 MODELS.md)
DEFAULT_GLM_MODELS = [
    "glm-5.3",
    "glm-5.3-flash",  # Coding Plan 套餐内两层家族的 worker 档
    "glm-5.2",
    "glm-4.7",        # Coding Plan 内预期被静默切到 5.3-flash(看 response_model 回显)
    "glm-4.5-air",
    "glm-4.5-flash",  # 已下线(2026-01-30),预期失败
]
DEFAULT_DEEPSEEK_MODELS = [
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "deepseek-chat",    # 已停用(2026-07-24),预期失败
    "deepseek-reasoner",  # 已停用(2026-07-24),预期失败
]
DEFAULT_GLM_WEBSEARCH_MODEL = "glm-5.3-flash"

REQUIRED_ENV_HINT = """\
[!] 未检测到任何 API key,未发起任何请求。密钥两级读取:环境变量优先,
    modelbench/keys.local.json 兜底。需要:
    GLM_API_KEY        智谱开放平台 key(https://bigmodel.cn/usercenter/proj-mgmt/apikeys)
    DEEPSEEK_API_KEY   DeepSeek key(https://platform.deepseek.com/api_keys)
可选:
    GLM_BASE_URL        默认 {glm_default}(Coding Plan key 用编程通道
                        {glm_coding},keys.local.json 里已配)
    DEEPSEEK_BASE_URL   默认 {ds_default}
    GLM_MODELS / DEEPSEEK_MODELS / GLM_WEBSEARCH_MODEL  覆盖默认候选(逗号分隔)
"""


# ---------------------------------------------------------------- HTTP 基础

def chat_completions_url(base_url: str) -> str:
    """把 base_url 规范成 .../chat/completions 完整端点。"""
    base = (base_url or "").strip().rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    return base + "/chat/completions"


def http_post_json(url: str, api_key: str, payload: dict, timeout: int = TIMEOUT_SECONDS) -> dict:
    """POST JSON,返回 {status, body(dict|str), error}。不抛网络异常。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
        },
        method="POST",
    )
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
        status = e.code
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        return {"status": None, "body": None,
                "error": "network: %r" % (e,), "latency_ms": round((time.time() - started) * 1000)}
    latency_ms = round((time.time() - started) * 1000)
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        body = raw[:500]
    return {"status": status, "body": body, "error": None, "latency_ms": latency_ms}


# ---------------------------------------------------------------- 请求构造

def glm_payload(model: str) -> dict:
    """GLM 最小 completion。5.3 系思考强制开启,只能 low effort;其余关思考省 token。"""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": MAX_TOKENS,
    }
    if model.startswith("glm-5.3"):
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = "low"
    else:
        payload["thinking"] = {"type": "disabled"}
    return payload


def deepseek_payload(model: str) -> dict:
    """DeepSeek 最小 completion。v4 系关思考,让 8 个 token 落在正文上。"""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": MAX_TOKENS,
    }
    if model.startswith("deepseek-v4"):
        payload["thinking"] = {"type": "disabled"}
    return payload


def glm_websearch_payload(model: str) -> dict:
    """带内置 web_search 工具的最小调用,验证 tools 格式(search_std ¥0.01/次)。"""
    return {
        "model": model,
        "messages": [{"role": "user", "content": "智谱GLM最新发布的旗舰模型叫什么?一句话。"}],
        "max_tokens": 256,
        "thinking": {"type": "disabled"},
        "tools": [
            {
                "type": "web_search",
                "web_search": {
                    "enable": True,             # 默认 false,必须显式 true
                    "search_engine": "search_std",
                    "search_result": True,      # 返回搜索来源,便于确认工具真的执行了
                },
            }
        ],
    }


# ---------------------------------------------------------------- 检查逻辑

def _extract(body) -> dict:
    """从 chat.completions 响应里抽展示字段(容错)。"""
    out = {"response_model": None, "finish_reason": None,
           "usage": None, "content_preview": None, "web_search_sources": None}
    if not isinstance(body, dict):
        return out
    out["response_model"] = body.get("model")
    choices = body.get("choices") or []
    if choices and isinstance(choices[0], dict):
        choice = choices[0]
        out["finish_reason"] = choice.get("finish_reason")
        msg = choice.get("message") or {}
        content = msg.get("content")
        if content:
            out["content_preview"] = str(content).replace("\n", " ")[:60]
        # web_search 来源:历史字段 message.web_search[];新版本可能改引用标注,如见到就计数
        ws = msg.get("web_search")
        if isinstance(ws, list):
            out["web_search_sources"] = len(ws)
    usage = body.get("usage")
    if isinstance(usage, dict):
        out["usage"] = {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
        }
    return out


def _err_text(body) -> str:
    """尽量从错误体里抠出人类可读信息。"""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)[:120]
        for key in ("message", "msg"):
            if body.get(key):
                return str(body[key])[:120]
    if isinstance(body, str) and body:
        return body.replace("\n", " ")[:120]
    return ""


def run_check(provider: str, model: str, base_url: str, api_key: str,
              payload: dict, check: str = "completion",
              endpoint: str = "") -> dict:
    url = chat_completions_url(base_url)
    res = http_post_json(url, api_key, payload)
    record = {
        "provider": provider,
        "endpoint": endpoint or base_url,
        "model": model,
        "check": check,
        "ok": res["status"] == 200,
        "http_status": res["status"],
        "error": res["error"] or (_err_text(res["body"]) if res["status"] != 200 else None),
        "latency_ms": res["latency_ms"],
        "rerouted": False,
    }
    record.update(_extract(res["body"]) if res["status"] == 200 else
                  {"response_model": None, "finish_reason": None, "usage": None,
                   "content_preview": None, "web_search_sources": None})
    # 静默切模型检测(Coding Plan 头号风险):响应 model 回显与请求不一致即标记
    if record.get("response_model") and record["response_model"] != model:
        record["rerouted"] = True

    # 兼容重试:个别模型不接受 thinking 参数(如旧名/未支持开关的版本)时,去掉再试一次
    if (res["status"] == 400 and "thinking" in payload
            and record["error"] and "think" in record["error"].lower()):
        retry_payload = {k: v for k, v in payload.items() if k != "thinking"}
        res2 = http_post_json(url, api_key, retry_payload)
        if res2["status"] == 200:
            record.update({
                "ok": True, "http_status": 200, "error": None,
                "latency_ms": res2["latency_ms"], "notes": "retried_without_thinking",
            })
            record.update(_extract(res2["body"]))
            if record.get("response_model") and record["response_model"] != model:
                record["rerouted"] = True
    return record


# ---------------------------------------------------------------- 输出

def fmt_width(text, width):
    text = str(text if text is not None else "")
    return text[:width].ljust(width)


def print_table(records):
    header = ["provider", "endpoint", "model", "check", "result", "http", "ms",
              "finish", "usage(p/c)", "preview / error"]
    widths = [8, 10, 22, 10, 6, 4, 6, 8, 11, 56]
    line = "  ".join(fmt_width(h, w) for h, w in zip(header, widths))
    print(line)
    print("-" * len(line))
    for r in records:
        usage = "-"
        if r.get("usage"):
            usage = "%s/%s" % (r["usage"]["prompt_tokens"], r["usage"]["completion_tokens"])
        preview = r.get("content_preview") or r.get("error") or ""
        if r.get("rerouted"):
            preview = "[REROUTED→%s] %s" % (r.get("response_model"), preview)
        if r.get("check") == "web_search" and r.get("web_search_sources") is not None:
            preview = "[sources=%s] %s" % (r["web_search_sources"], preview)
        cells = [
            r["provider"], r.get("endpoint", ""), r["model"], r["check"],
            "OK" if r["ok"] else "FAIL",
            r["http_status"] if r["http_status"] is not None else "-",
            r["latency_ms"], r.get("finish_reason") or "-", usage, preview,
        ]
        print("  ".join(fmt_width(c, w) for c, w in zip(cells, widths)))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    glm_key = (keyconfig.get("GLM_API_KEY") or "").strip()
    ds_key = (keyconfig.get("DEEPSEEK_API_KEY") or "").strip()
    glm_base_cfg = (keyconfig.get("GLM_BASE_URL") or "").strip() or GLM_BASE_URL_DEFAULT
    ds_base = (keyconfig.get("DEEPSEEK_BASE_URL") or "").strip() or DEEPSEEK_BASE_URL_DEFAULT

    if not glm_key and not ds_key:
        sys.stdout.write(REQUIRED_ENV_HINT.format(
            glm_default=GLM_BASE_URL_DEFAULT, glm_coding=GLM_CODING_BASE_URL,
            ds_default=DEEPSEEK_BASE_URL_DEFAULT))
        return 2

    glm_models = [m.strip() for m in os.environ.get("GLM_MODELS", "").split(",") if m.strip()] \
        or DEFAULT_GLM_MODELS
    ds_models = [m.strip() for m in os.environ.get("DEEPSEEK_MODELS", "").split(",") if m.strip()] \
        or DEFAULT_DEEPSEEK_MODELS
    glm_ws_model = os.environ.get("GLM_WEBSEARCH_MODEL", "").strip() or DEFAULT_GLM_WEBSEARCH_MODEL

    records = []
    print("preflight @ %s (UTC)" % datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"))
    if glm_key:
        # GLM 双端点实测:Coding Plan key 在 paas/v4 与编程通道的行为都要坐实
        # (套餐额度只在编程通道有效;静默切模型看 response_model 回显)。
        glm_endpoints = [("cfg", glm_base_cfg)]
        if glm_base_cfg.rstrip("/") != GLM_BASE_URL_DEFAULT:
            glm_endpoints.append(("std", GLM_BASE_URL_DEFAULT))
        if glm_base_cfg.rstrip("/") != GLM_CODING_BASE_URL:
            glm_endpoints.append(("coding", GLM_CODING_BASE_URL))
        for ep_label, ep_base in glm_endpoints:
            print("GLM[%s] base=%s models=%s" % (ep_label, ep_base, glm_models))
            for model in glm_models:
                records.append(run_check("glm", model, ep_base, glm_key,
                                         glm_payload(model), endpoint=ep_label))
        # web_search 只在配置端点测一次(省费/省积分)
        records.append(run_check("glm", glm_ws_model, glm_base_cfg, glm_key,
                                 glm_websearch_payload(glm_ws_model),
                                 check="web_search", endpoint="cfg"))
    else:
        print("[skip] GLM 侧未配置 GLM_API_KEY")
    if ds_key:
        print("DeepSeek  base=%s models=%s" % (ds_base, ds_models))
        for model in ds_models:
            records.append(run_check("deepseek", model, ds_base, ds_key,
                                     deepseek_payload(model), endpoint="cfg"))
    else:
        print("[skip] DeepSeek 侧未配置 DEEPSEEK_API_KEY")

    print()
    print_table(records)

    ok_count = sum(1 for r in records if r["ok"])
    rerouted = [r for r in records if r.get("rerouted")]
    total = len(records)
    print()
    print("summary: %d/%d checks passed; rerouted(响应模型≠请求模型): %d"
          % (ok_count, total, len(rerouted)))
    for r in rerouted:
        print("  [rerouted] %s[%s] %s → %s"
              % (r["provider"], r.get("endpoint", ""), r["model"],
                 r.get("response_model")))

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "providers": {
            "glm": {"configured": bool(glm_key),
                    "base_url": glm_base_cfg if glm_key else None,
                    "models": glm_models if glm_key else [],
                    "websearch_model": glm_ws_model if glm_key else None},
            "deepseek": {"configured": bool(ds_key), "base_url": ds_base if ds_key else None,
                         "models": ds_models if ds_key else []},
        },
        "summary": {"total": total, "ok": ok_count, "failed": total - ok_count,
                    "rerouted": len(rerouted)},
        "results": records,
    }
    os.makedirs(os.path.dirname(RESULTS_PATH), exist_ok=True)
    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print("written: %s" % RESULTS_PATH)
    return 0 if ok_count == total else 1


if __name__ == "__main__":
    sys.exit(main())
