"""DPswarm CLI。

用法：
  dpswarm init [--dir DIR] [--spec JSON]        初始化工作区（事件日志 + 默认目录）
  dpswarm run --task "..." --mock SCRIPT.json   MockProvider 演示完整链
  dpswarm run --task "..." --model PROVIDER/MODEL [--base-url URL] [--api-key KEY]
                                                OpenAI 兼容网关真跑（如 GLM Coding Plan）
  dpswarm status [--dir DIR]                    投影快照 + 观测摘要
  dpswarm replay [--dir DIR]                    从事件日志离线重建并全量重验（§9.1）
  dpswarm seal [--dir DIR] [--timeout S]        手动封存三段式
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import observation
from .control import ControlPlane, ControlPlaneError
from .types import Level, ModelCatalog, ModelFacts, RootExecutionSpec


def _workspace(dir_path: Optional[str]) -> Path:
    base = Path(dir_path) if dir_path else Path(".dpswarm")
    base.mkdir(parents=True, exist_ok=True)
    return base


def _default_catalog() -> ModelCatalog:
    cat = ModelCatalog()
    cat.register(ModelFacts("mock", "grok", Level.S,
                            aa_dimensional={"coding": 9.0, "reasoning": 9.2, "overall": 9.1}))
    cat.register(ModelFacts("mock", "glm", Level.A,
                            aa_dimensional={"coding": 8.5, "reasoning": 8.3, "overall": 8.4}))
    cat.register(ModelFacts("mock", "kimi", Level.B,
                            aa_dimensional={"coding": 7.8, "reasoning": 7.6, "overall": 7.7}))
    return cat


def cmd_init(args: argparse.Namespace) -> int:
    ws = _workspace(args.dir)
    spec = RootExecutionSpec(**json.loads(args.spec)) if args.spec else RootExecutionSpec()
    cp = ControlPlane(spec=spec, store_path=ws / "events.jsonl", catalog=_default_catalog())
    print(json.dumps({"workspace": str(ws), "spec_revision": cp.proj.spec.revision,
                      "root_item": cp._root_item_id()}, ensure_ascii=False))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .orchestrator import Orchestrator
    ws = _workspace(args.dir)
    cp = ControlPlane(store_path=ws / "events.jsonl", catalog=_default_catalog())

    if args.mock:
        script = json.loads(Path(args.mock).read_text(encoding="utf-8"))
        from .providers import MockProvider
        provider = MockProvider(script=script)
        lead = None
    else:
        if not args.model:
            print("--model PROVIDER/MODEL required (or --mock)", file=sys.stderr)
            return 2
        p, m = args.model.split("/", 1)
        cat = cp.catalog
        if cat.resolve(p, m) is None:
            cat.register(ModelFacts(p, m, Level.A, aa_dimensional={"coding": 8.0, "overall": 8.0}))
        from .providers import OpenAICompatProvider
        provider = OpenAICompatProvider(base_url=args.base_url, api_key=args.api_key)
        from .types import ModelRoute
        lead = ModelRoute(p, m, level=cat.resolve(p, m).level)

    orch = Orchestrator(cp, provider, store_dir=ws / "packages", lead_route=lead)
    result = orch.run_task(args.task)
    cp.begin_seal("root")
    cp.begin_settlement("root")
    cp.finish_seal("root")
    print(json.dumps(result, ensure_ascii=False, indent=1))
    report = observation.summarize_events(cp.store.read_all())
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    ws = _workspace(args.dir)
    cp = ControlPlane(store_path=ws / "events.jsonl", catalog=_default_catalog())
    print(json.dumps(cp.snapshot(), ensure_ascii=False, indent=1))
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """离线 replay：从日志重建投影并逐事件重验不变量（§9.1 可对账性）。"""
    from . import invariants, state
    ws = _workspace(args.dir)
    store_path = ws / "events.jsonl"
    if not store_path.exists():
        print("no event log", file=sys.stderr)
        return 2
    events = ControlPlane(store_path=store_path).store.read_all()
    proj = state.Projection()
    checked = 0
    for ev in events:
        proj = invariants.check_event(proj, ev)  # 不合法会抛 InvariantViolation
        checked += 1
    print(json.dumps({"events": checked, "graph_revision": proj.graph_revision,
                      "active_points": proj.active_points,
                      "work_items": len(proj.work_items)}, ensure_ascii=False))
    return 0


def cmd_seal(args: argparse.Namespace) -> int:
    ws = _workspace(args.dir)
    cp = ControlPlane(store_path=ws / "events.jsonl", catalog=_default_catalog())
    cp.begin_seal("root")
    cp.begin_settlement("root")
    cp.finish_seal("root", timed_out=False)
    print(json.dumps(cp.snapshot()["seal_phase"], ensure_ascii=False))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="dpswarm")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init")
    p.add_argument("--dir")
    p.add_argument("--spec")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("run")
    p.add_argument("--task", required=True)
    p.add_argument("--mock", help="MockProvider script JSON path")
    p.add_argument("--model", help="PROVIDER/MODEL for OpenAI-compat gateway")
    p.add_argument("--base-url")
    p.add_argument("--api-key")
    p.add_argument("--dir")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("status")
    p.add_argument("--dir")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("replay")
    p.add_argument("--dir")
    p.set_defaults(fn=cmd_replay)

    p = sub.add_parser("seal")
    p.add_argument("--dir")
    p.set_defaults(fn=cmd_seal)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
