"""Output visibility evidence, with no containers or model requests."""
import json
from pathlib import Path
import sys

import pytest


REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from modelbench.swe_verified_20260903.environment import SWEEnvironment


@pytest.mark.parametrize("extra", [-1, 0, 1])
def test_output_limits_report_characters_and_keep_complete_capture(tmp_path, monkeypatch, extra):
    environment = SWEEnvironment({"instance_id": "pallets__flask-5014", "base_commit": "a" * 40}, tmp_path)
    stdout, stderr = "o" * (40000 + extra), "e" * (12000 + extra)
    captured = {"stdout": stdout, "stderr": stderr, "exit_code": 7,
                "timed_out": False, "duration_seconds": 0.1}
    monkeypatch.setattr(environment, "_exec", lambda *args, **kwargs: captured)
    result = environment.run("fixture command")
    record = json.loads((tmp_path / "commands/00001.json").read_text(encoding="utf-8"))
    assert record == {"command": "fixture command", **captured}
    for field, original, limit in (("stdout", stdout, 40000), ("stderr", stderr, 12000)):
        assert result[field] == original[-limit:]
        assert result[field + "_truncated"] is (extra > 0)
        assert result[field + "_captured_chars"] == len(original)
        assert result[field + "_returned_chars"] == len(result[field]) == min(len(original), limit)
    assert result["exit_code"] == 7 and result["timed_out"] is False
    assert captured == {key: record[key] for key in captured}  # Raw capture was not shortened in place.


def test_counts_are_unicode_characters_not_encoded_bytes(tmp_path, monkeypatch):
    environment = SWEEnvironment({"instance_id": "pallets__flask-5014", "base_commit": "a" * 40}, tmp_path)
    captured = {"stdout": "é" * 40001, "stderr": "界" * 12001,
                "exit_code": 0, "timed_out": False, "duration_seconds": 0.1}
    monkeypatch.setattr(environment, "_exec", lambda *args, **kwargs: captured)
    result = environment.run("unicode fixture")
    assert result["stdout_captured_chars"] == 40001
    assert result["stdout_returned_chars"] == 40000
    assert result["stderr_captured_chars"] == 12001
    assert result["stderr_returned_chars"] == 12000
    assert result["stdout_truncated"] is True and result["stderr_truncated"] is True
    assert len(captured["stdout"].encode("utf-8")) == 80002
    assert len(captured["stderr"].encode("utf-8")) == 36003


def test_failure_text_never_rewrites_shell_status(tmp_path, monkeypatch):
    environment = SWEEnvironment({"instance_id": "pallets__flask-5014", "base_commit": "a" * 40}, tmp_path)
    captured = {"stdout": "2 failed, 2 passed\n", "stderr": "", "exit_code": 0,
                "timed_out": False, "duration_seconds": 0.1}
    monkeypatch.setattr(environment, "_exec", lambda *args, **kwargs: captured)
    result = environment.run("pytest | tail -40")
    assert result["exit_code"] == 0 and result["stdout"] == captured["stdout"]
    assert result["stdout_truncated"] is False and result["stderr_truncated"] is False
    assert result["stderr_captured_chars"] == result["stderr_returned_chars"] == 0


def test_capture_limit_failure_keeps_status_and_counts_only_available_text(tmp_path, monkeypatch):
    environment = SWEEnvironment({"instance_id": "pallets__flask-5014", "base_commit": "a" * 40}, tmp_path)
    captured = {"stdout": "captured prefix", "stderr": "capture limit reached",
                "exit_code": 125, "timed_out": False, "duration_seconds": 0.1,
                "output_limit_exceeded": True}
    monkeypatch.setattr(environment, "_exec", lambda *args, **kwargs: captured)
    result = environment.run("oversized output fixture")
    assert result["exit_code"] == 125 and result["output_limit_exceeded"] is True
    assert result["stdout_captured_chars"] == len(captured["stdout"])
    assert result["stderr_captured_chars"] == len(captured["stderr"])
