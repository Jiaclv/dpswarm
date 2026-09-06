"""Candidate import and standalone grader entrypoints; no Docker or model calls."""
import importlib.util
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from modelbench.swe_verified_20260903 import environment
from modelbench.swe_verified_20260903.worktree_probe import PROBE_SCRIPT


@pytest.mark.parametrize("entrypoint", ["package", "legacy_direct"])
def test_observer_loads_probe_for_both_candidate_imports(tmp_path, monkeypatch, entrypoint):
    module = environment
    if entrypoint == "legacy_direct":
        source = Path(environment.__file__)
        monkeypatch.syspath_prepend(str(source.parent))
        spec = importlib.util.spec_from_file_location("environment_legacy_fixture", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert not module.__package__
    instance = {"instance_id": "pallets__flask-5014", "base_commit": "a" * 40}
    candidate = module.SWEEnvironment(instance, tmp_path)
    added = {"kind": "file", "bytes": 2, "sha256": "content-hash"}
    snapshots = iter([{}, {"new.bin": added}])
    commands = []

    def execute(command, **kwargs):
        commands.append((command, kwargs))
        return {"stdout": json.dumps(next(snapshots))}

    monkeypatch.setattr(candidate, "_exec", execute)
    baseline = candidate.observe_worktree()
    changed = candidate.observe_worktree()
    assert baseline["nonempty_delta"] is False
    assert changed["nonempty_delta"] is True
    assert changed["changed_files"] == {"new.bin": {"before": None, "after": added}}
    assert changed["baseline_sha256"] == baseline["state_sha256"]
    assert changed["measurement"] == "direct_file_bytes_v1"
    assert all(shlex.split(command) == ["python", "-I", "-c", PROBE_SCRIPT]
               for command, _ in commands)
    assert all(options == {"timeout": 120, "check": True} for _, options in commands)


def test_standalone_grader_reaches_contract_check_without_probe_module(tmp_path):
    # Reproduce the controller mount: environment.py exists alone at /bridge.
    script = tmp_path / "environment.py"
    shutil.copyfile(environment.__file__, script)
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"grader_contract": None}), encoding="utf-8")
    assert not (tmp_path / "worktree_probe.py").exists()
    result = subprocess.run([sys.executable, "-I", "-B", str(script), "_grade", str(request)],
                            cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert result.returncode == 1
    assert "grading requires a frozen grader_contract" in result.stderr
    assert "ImportError" not in result.stderr
    assert "ModuleNotFoundError" not in result.stderr
