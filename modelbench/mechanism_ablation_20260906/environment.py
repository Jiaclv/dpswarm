"""C1 candidate shell: preserve pipeline failures in one execution."""
from __future__ import annotations

import re
from uuid import uuid4

from modelbench.minimal_value_20260905.environment import ValueEnvironment, EnvironmentError
from modelbench.swe_verified_20260903.environment import _json


def pipeline_command(command, marker):
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command must be nonempty text")
    # Capture failed pipelines before a later successful echo masks them.
    # ERR records unhandled failures, not pytest identities. No errexit/replay.
    trap = ('__c1_rc=$? __c1_ps=("${PIPESTATUS[@]}"); '
            + "printf \"\\n" + marker + ":%s:\" \"$__c1_rc\" >&2; "
            + 'printf "%s," "${__c1_ps[@]}" >&2; printf "\\n" >&2')
    return "set -o pipefail\nset -E\ntrap '" + trap + "' ERR\n" + command


class AblationEnvironment(ValueEnvironment):
    def run(self, command, timeout=None):
        with self._lock:
            if self._quiesced:
                raise EnvironmentError("candidate execution is forbidden after quiesce")
            marker = "__C1_FAILURE_" + uuid4().hex
            actual = pipeline_command(command, marker)
            self._command_seq += 1
            result = self._exec(actual, timeout=timeout)
            if result["timed_out"]:
                self.close()
            failures = [{"exit_code": int(match.group(1)),
                         "pipeline_statuses": [int(x) for x in match.group(2).split(",") if x]}
                        for match in re.finditer(re.escape(marker) + r":(\d+):([\d,]+)", result["stderr"])]
            result["observed_shell_failures"] = failures
            result["test_status"] = "mixed_or_unknown" if failures and result["exit_code"] == 0 else "not_inferred"
            result["command_outcome"] = ("completed_with_observed_failures" if failures and result["exit_code"] == 0
                                          else "failed" if result["exit_code"] else "completed")
            result["exit_code_semantics"] = "final_bash_pipefail_status; inspect_observed_shell_failures_for_prior_failures"
            result["failure_observation_scope"] = "ERR_unhandled_failures_only; no_test_runner_attribution"
            result["pipeline_failure_propagation"] = True
            _json(self.run_dir / "commands" / f"{self._command_seq:05d}.json",
                  {"command": command, "executed_command": actual, **result})
            visible = dict(result)
            for field, limit in (("stdout", 40000), ("stderr", 12000)):
                captured = result[field]
                visible[field] = captured[-limit:]
                visible[field + "_truncated"] = bool(result.get(field + "_truncated")) or len(captured) > limit
                visible[field + "_captured_chars"] = len(captured)
                visible[field + "_returned_chars"] = len(visible[field])
            return visible
