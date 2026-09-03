"""keyconfig.py — modelbench 密钥两级读取（stdlib-only，供 providers/preflight 共用）。

优先级：环境变量 > modelbench/keys.local.json。文件不入库、不上传；
仅本机实验使用。keys.local.json 字段：GLM_API_KEY / GLM_BASE_URL /
DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

KEYS_FILE = Path(__file__).resolve().parent / "keys.local.json"


def _file_value(name: str) -> Optional[str]:
    try:
        data = json.loads(KEYS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get(name)
    return str(value).strip() if value else None


def get(name: str) -> Optional[str]:
    """读密钥/端点配置：env 优先，keys.local.json 兜底；都没有返回 None。"""
    return os.environ.get(name, "").strip() or _file_value(name)
