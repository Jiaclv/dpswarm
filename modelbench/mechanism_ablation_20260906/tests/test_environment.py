"""Local Bash fixtures only; one invocation and preserved host capture."""
import json
from pathlib import Path
import shutil
import subprocess
import threading

import pytest

from modelbench.mechanism_ablation_20260906.environment import AblationEnvironment, EnvironmentError


@pytest.fixture
def environment(tmp_path):
    env = object.__new__(AblationEnvironment)
    env._lock = threading.RLock()
    env._quiesced = False
    env._command_seq = 0
    env.run_dir = tmp_path
    return env


@pytest.mark.parametrize("status", [0, 1, 5])
def test_pipeline_test_status_and_single_execution(environment, tmp_path, monkeypatch, status):
    git_bash = next((p for p in (Path("C:/Program Files/Git/bin/bash.exe"), Path("K:/Git/bin/bash.exe")) if p.is_file()), Path("missing"))
    bash = str(git_bash) if git_bash.is_file() else shutil.which("bash")
    assert bash, "Offline Bash fixture requires the local Bash used by the shell contract"
    marker = tmp_path / "invocations.txt"
    command = ("pytest_fixture() { printf x >> " + repr(marker.as_posix()) + "; "
               "printf 'fixture test output\\n'; return " + str(status) + "; }\npytest_fixture | tail -n 1")
    executions = []
    def execute(actual, timeout):
        executions.append(actual)
        proc = subprocess.run([bash, "-c", actual], capture_output=True, text=True, timeout=10)
        return {"stdout": proc.stdout, "stderr": proc.stderr, "exit_code": proc.returncode,
                "timed_out": False, "duration_seconds": 0.01}
    monkeypatch.setattr(environment, "_exec", execute)
    result = environment.run(command, timeout=10)
    assert result["exit_code"] == status
    assert marker.read_text() == "x" and len(executions) == 1
    raw = json.loads((tmp_path / "commands/00001.json").read_text(encoding="utf-8"))
    assert raw["command"] == command and raw["executed_command"] == executions[0]
    assert raw["stdout"] == "fixture test output\n"


def test_capture_is_preserved_and_common_visible_limits_unchanged(environment, tmp_path, monkeypatch):
    output = "a" * 41000
    monkeypatch.setattr(environment, "_exec", lambda *a, **k: {
        "stdout": output, "stderr": "e" * 13000, "exit_code": 1, "timed_out": False})
    result = environment.run("fixture | tail", 1)
    raw = json.loads((tmp_path / "commands/00001.json").read_text(encoding="utf-8"))
    assert raw["stdout"] == output
    assert len(result["stdout"]) == 40000 and len(result["stderr"]) == 12000
    assert result["stdout_captured_chars"] == 41000


def test_quiesced_environment_never_executes(environment, monkeypatch):
    environment._quiesced = True
    monkeypatch.setattr(environment, "_exec", lambda *a, **k: pytest.fail("Forbidden after quiesce"))
    with pytest.raises(EnvironmentError, match="quiesce"):
        environment.run("pytest | tail")


@pytest.mark.parametrize("name", ["pytest_fixture", "grep_fixture"])
def test_later_success_does_not_erase_failure_or_infer_test_outcome(environment, monkeypatch, name):
    bash = next(str(p) for p in (Path("C:/Program Files/Git/bin/bash.exe"), Path("K:/Git/bin/bash.exe")) if p.is_file())
    count = []
    def execute(command, timeout):
        count.append(command)
        proc = subprocess.run([bash, "-c", command], capture_output=True, text=True, timeout=10)
        return {"stdout": proc.stdout, "stderr": proc.stderr, "exit_code": proc.returncode, "timed_out": False}
    monkeypatch.setattr(environment, "_exec", execute)
    result = environment.run(name + "() { return 1; }; " + name + " | tail -n 1; echo DONE", 10)
    assert len(count) == 1 and result["exit_code"] == 0
    assert any(item["pipeline_statuses"] == [1, 0] for item in result["observed_shell_failures"])
    assert result["test_status"] == "mixed_or_unknown"
    assert result["command_outcome"] == "completed_with_observed_failures"
