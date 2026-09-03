"""Offline contract checks; optional real Docker checks use no candidate model."""
import json
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import environment as envmod
from environment import EnvironmentError, SWEEnvironment, image_name
from prepare_dataset import select_metadata


PUBLIC = {"instance_id": "pallets__flask-5014", "repo": "pallets/flask", "base_commit": "a" * 40,
          "problem_statement": "Public issue", "version": "2.3"}


def test_selection_is_metadata_only_and_order_independent():
    class MetadataOnly(dict):
        def __getitem__(self, name):
            assert name in ("repo", "instance_id")
            return super().__getitem__(name)
    rows = [MetadataOnly(repo=f"repo/{i}", instance_id=f"repo__r-{i * 2 + j}",
                         patch="NEVER READ", difficulty="NEVER READ") for i in range(12) for j in range(2)]
    selected = select_metadata(rows)
    assert selected == select_metadata(list(reversed(rows)))
    assert len(selected) == len({r["repo"] for r in selected}) == 10
    assert all(set(row) == {"repo", "instance_id"} for row in selected)


@pytest.mark.parametrize("field", sorted(envmod.FORBIDDEN_FIELDS))
def test_candidate_private_fields_rejected(tmp_path, field):
    with pytest.raises(ValueError, match="private"):
        SWEEnvironment({**PUBLIC, field: "private"}, tmp_path)


def test_image_name_matches_official_remote_convention():
    assert image_name("pallets__flask-5014") == "swebench/sweb.eval.x86_64.pallets_1776_flask-5014:latest"
    with pytest.raises(ValueError):
        image_name("x; touch /bad")


def test_fork_uses_explicit_snapshot_and_limits(tmp_path, monkeypatch):
    contract = {"input_artifacts": {"x": "y"}, "environment_sha": "hash"}
    parent = SWEEnvironment(PUBLIC, tmp_path / "parent", memory="3g", cpus=2, grader_contract=contract)
    monkeypatch.setattr(SWEEnvironment, "start", lambda self: self)
    child = parent.fork(tmp_path / "worker", baseline_patch="snapshot")
    assert child.baseline_patch == "snapshot"
    assert child.memory == "3g" and child.cpus == 2
    assert child.run_dir != parent.run_dir
    assert child.grader_contract == contract and child.grader_contract is not parent.grader_contract
    with pytest.raises(TypeError):
        parent.fork(tmp_path / "missing")


def test_command_timeout_closes_owned_environment(tmp_path, monkeypatch):
    environment = SWEEnvironment(PUBLIC, tmp_path)
    environment.container_id = "owned"
    monkeypatch.setattr(environment, "_exec", lambda *a, **kw: {"stdout": "", "stderr": "", "exit_code": 124, "timed_out": True})
    closed = []
    monkeypatch.setattr(environment, "close", lambda: closed.append(True))
    assert environment.run("sleep 99")["timed_out"]
    assert closed == [True]


def test_workspace_permission_setup_uses_each_owner_without_symlink_chmod(tmp_path, monkeypatch):
    environment = SWEEnvironment(PUBLIC, tmp_path)
    commands = []
    def execute(command, **kwargs):
        commands.append((command, kwargs))
        return {"stdout": "0\n1000\n4242\n" if len(commands) == 1 else "", "exit_code": 0}
    monkeypatch.setattr(environment, "_exec", execute)
    environment._prepare_workspace_permissions()
    assert [kw["user"] for _, kw in commands] == ["0:0", "0:0", "1000:1000", "4242:4242"]
    assert all("! -type l" in command and "-xdev" in command for command, _ in commands)
    for uid, (command, _) in zip((0, 1000, 4242), commands[1:]):
        assert f"-uid {uid}" in command and "chmod a+rwX -- {} +" in command
        assert "-R" not in command and "-L" not in command
    record = json.loads((tmp_path / "permissions.json").read_text())
    assert record["owner_uids"] == [0, 1000, 4242]
    assert record["candidate_user"] == "1000:1000" and record["added_capabilities"] == []


def test_workspace_invalid_owner_output_fails_before_any_owner_exec(tmp_path, monkeypatch):
    environment = SWEEnvironment(PUBLIC, tmp_path)
    commands = []
    def execute(command, **kwargs):
        commands.append(command)
        return {"stdout": "0\ninvalid;command\n", "exit_code": 0}
    monkeypatch.setattr(environment, "_exec", execute)
    with pytest.raises(EnvironmentError, match="invalid UIDs"):
        environment._prepare_workspace_permissions()
    assert len(commands) == 1 and not (tmp_path / "permissions.json").exists()


def test_command_capture_is_bounded_without_docker(monkeypatch):
    original_popen = subprocess.Popen
    def fake_docker(_args, **kwargs):
        return original_popen([sys.executable, "-c", "print('x'*262144)"], **kwargs)
    monkeypatch.setattr(envmod.subprocess, "Popen", fake_docker)
    result = envmod._docker(["unused"], output_limit=1024, check=False)
    assert result.output_limit_exceeded is True
    assert len(result.stdout.encode()) <= 1024


def test_close_rejects_foreign_ownership(tmp_path, monkeypatch):
    environment = SWEEnvironment(PUBLIC, tmp_path)
    environment.container_id = "foreign"
    calls = []
    def docker(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps([{"Config": {"Labels": {"dpswarm.swe.owner": "someone-else"}}}]), "")
    monkeypatch.setattr(envmod, "_docker", docker)
    with pytest.raises(EnvironmentError, match="ownership"):
        environment.close()
    assert len(calls) == 1 and calls[0][0] == "inspect"


def test_close_preserves_identity_on_unknown_inspect_failure(tmp_path, monkeypatch):
    environment = SWEEnvironment(PUBLIC, tmp_path)
    environment.container_id = "owned"
    monkeypatch.setattr(envmod, "_docker", lambda args, **kw: subprocess.CompletedProcess(args, 1, "", "daemon unavailable"))
    with pytest.raises(EnvironmentError, match="cannot verify"):
        environment.close()
    assert environment.container_id == "owned" and environment._closed is False


def test_frozen_final_patch_conflict_rejected_before_grader(tmp_path):
    environment = SWEEnvironment(PUBLIC, tmp_path)
    (tmp_path / "final.patch").write_text("already frozen", encoding="utf-8")
    with pytest.raises(EnvironmentError, match="frozen"):
        environment.grade("different")


def test_cleanup_failure_is_reported(tmp_path, monkeypatch):
    record = tmp_path / "containers.json"
    record.write_text(json.dumps(["owned"]))
    def docker(args, **kwargs):
        if args[0] == "inspect":
            return subprocess.CompletedProcess(args, 0, json.dumps([{"Config": {"Labels": {"dpswarm.swe.owner": "owner"}}}]), "")
        return subprocess.CompletedProcess(args, 1, "", "simulated removal failure")
    monkeypatch.setattr(envmod, "_docker", docker)
    errors = envmod._cleanup_recorded(record, "owner")
    assert len(errors) == 1 and "simulated removal failure" in errors[0]


def test_missing_or_changed_grader_contract_rejected(monkeypatch):
    with pytest.raises(EnvironmentError, match="requires"):
        envmod._verify_grader_contract(None)
    contract = {"input_artifacts": {name: "expected" for name in envmod.GRADER_INPUTS}, "environment_sha": "source"}
    monkeypatch.setattr(envmod, "_file_sha", lambda _path: "different")
    with pytest.raises(EnvironmentError, match="artifact changed"):
        envmod._verify_grader_contract(contract)


@pytest.mark.parametrize("patch", ["", " \n\t"])
def test_empty_patch_is_candidate_failure_not_infrastructure(patch):
    status = {"completed": False, "resolved": False}
    result = envmod._classify_official_status(status, patch)
    assert result["failure_kind"] == "candidate_empty_patch"
    assert result["completed"] is False and result["resolved"] is False
    assert result["official_status"] == status and result["official_completed"] is False
    assert "infrastructure_error" not in result


def test_nonempty_incomplete_grade_stays_unknown():
    result = envmod._classify_official_status({"completed": False, "resolved": False}, "nonempty patch")
    assert result["resolved"] is None and result["infrastructure_error"]


@pytest.mark.parametrize("instance_id", ["pallets__flask-5014", "matplotlib__matplotlib-25122"])
def test_real_docker_isolation_fork_and_delta(instance_id):
    if not (envmod.OFFICIAL / "images" / (instance_id + ".json")).exists():
        pytest.skip("instance image has not been prepared")
    records = json.loads((envmod.OFFICIAL / "selected_public.json").read_text(encoding="utf-8"))
    instance = next(r for r in records if r["instance_id"] == instance_id)
    root = envmod.OFFICIAL / "environment_tests" / uuid.uuid4().hex
    parent = SWEEnvironment(instance, root / "lead")
    worker = None
    try:
        parent.start()
        assert parent.run("id -u")["stdout"].strip() == "1000"
        assert parent.run("test ! -e /var/run/docker.sock && test ! -e /eval.sh && test ! -e /shared")["exit_code"] == 0
        info = json.loads(envmod._docker(["inspect", parent.container_id]).stdout)[0]
        assert info["HostConfig"]["NetworkMode"] == "none" and info["Mounts"] == []
        assert info["HostConfig"]["CapDrop"] == ["ALL"] and info["HostConfig"]["CapAdd"] in (None, [])
        assert "no-new-privileges" in info["HostConfig"]["SecurityOpt"]
        assert parent.run("grep '^CapEff:' /proc/self/status")["stdout"].strip().split()[-1] == "0000000000000000"
        assert info["HostConfig"]["Memory"] == 3 * 1024**3
        permissions = json.loads((parent.run_dir / "permissions.json").read_text())
        assert permissions["candidate_user"] == "1000:1000" and permissions["added_capabilities"] == []
        if instance_id.startswith("matplotlib__"):
            assert any(uid != 0 for uid in permissions["owner_uids"])
        assert parent.export_patch() == ""
        assert parent.run("printf 'lead snapshot\\n' > dpswarm_lead_probe.txt")["exit_code"] == 0
        snapshot = parent.export_patch()
        parent.run("printf 'later lead change\\n' > dpswarm_later_probe.txt")
        worker = parent.fork(root / "worker", baseline_patch=snapshot)
        assert worker.run("test -f dpswarm_lead_probe.txt && test ! -f dpswarm_later_probe.txt")["exit_code"] == 0
        assert worker.export_patch(delta=True) == ""
        worker.run("printf 'worker only\\n' > dpswarm_worker_probe.txt")
        delta = worker.export_patch(delta=True)
        assert "dpswarm_worker_probe.txt" in delta and "dpswarm_lead_probe.txt" not in delta
        assert parent.run("test ! -e dpswarm_worker_probe.txt")["exit_code"] == 0
        worker.close()
        parent.apply_patch(delta)
        assert parent.run("test -e dpswarm_worker_probe.txt && test -e dpswarm_later_probe.txt")["exit_code"] == 0
    finally:
        if worker:
            worker.close()
        parent.close()
