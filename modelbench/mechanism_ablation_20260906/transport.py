"""C1 transport: isolate native Codex tool exposure; preserve strict response checks."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import uuid4

from modelbench.big_budget_team_20260906.transport import CodingPlanTransport, read_transport_policy

SOURCE = Path(__file__).resolve().parents[1] / "big_budget_team_20260906" / "transport.py"
_spec = importlib.util.spec_from_file_location("_c1_coding_" + uuid4().hex, SOURCE)
_runtime = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_runtime)
_original_load = _runtime._base._load_v2

# These feature names were verified against this host's installed Codex CLI.
# The candidate protocol's tools are JSON text, executed only by the outer loop.
DISABLED_NATIVE_FEATURES = (
    "apps", "plugins", "browser_use", "computer_use", "image_generation",
    "view_image", "workspace_dependencies", "memories", "skill_search",
)
NATIVE_TOOL_POLICY = {
    "protocol": "c1-codex-external-text-tools-only-v1",
    "extra_disabled_features": list(DISABLED_NATIVE_FEATURES),
    "mcp_servers": {}, "native_execution_allowed": False,
    "unexpected_native_tool_response": "reject_entire_call_preserve_usage_no_protocol_actions",
}


def constrain_codex_argv(argv):
    result = list(argv)
    if not result or result[-1] != "-" or "exec" not in result or "--ignore-user-config" not in result:
        raise ValueError("Unexpected Codex command; cannot bind C1 tool policy")
    extra = [item for feature in DISABLED_NATIVE_FEATURES for item in ("--disable", feature)]
    extra += ["-c", "mcp_servers={}"]
    result[-1:-1] = extra
    return result


def _load_v2():
    runtime = _original_load()
    original_codex = runtime.original.ExperimentTransport._codex

    def codex(self, record, messages, folder):
        # SweTransport installs its artifact-producing callback before this
        # method. Passing the constrained argv into it records the actual argv,
        # rather than changing a process after its request receipt was written.
        run = runtime.original._run_codex_process

        def constrained(argv, **kwargs):
            return run(constrain_codex_argv(argv), **kwargs)

        record["native_tool_policy"] = dict(NATIVE_TOOL_POLICY)
        runtime.original._run_codex_process = constrained
        return original_codex(self, record, messages, folder)

    runtime.original.ExperimentTransport._codex = codex
    return runtime


_runtime._base._load_v2 = _load_v2


class AblationTransport(_runtime.CodingPlanTransport, CodingPlanTransport):
    """Use a private dependency graph while retaining provider-gate isinstance.

    Both parents have the same frozen public interface. Execution resolves to
    the private graph; no historical module's hooks or files are changed.
    The inherited strict Codex event scan rejects every native tool event before
    parsing the terminal protocol text, while retaining observed call usage.
    """
