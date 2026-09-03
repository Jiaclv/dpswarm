import copy
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

recovery = importlib.import_module("modelbench.team_runtime_v2.recovery")
MODELS = {"planner": "gpt-5.6-sol", "executor": "glm-5.3", "verifier": "gpt-5.6-terra"}


def control_fixture(path):
    control = recovery.RecoverableControl(path, MODELS, "fixture-task")
    handle = control.start_role("executor", "initial")
    record = {"call_id": "call-a", "role": "executor", "model_requested": "glm-5.3", "task_id": "fixture-task",
              "input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 12,
              "total_tokens": 112, "reasoning_tokens": None, "wall_seconds": .1,
              "stop_reason": "tool_calls", "error": None}
    control.record_call(handle, record)
    return control, handle, record


def test_real_cp_restore_preserves_calls_submissions_fences_and_journal(tmp_path):
    control, handle, record = control_fixture(tmp_path)
    control.submit_role(handle, {"fixture": "submitted, not accepted"})
    expected_usage = control.get_usage()
    control.cp.close()  # Simulated process exit, without graceful termination.
    saved = {p: p.read_bytes() for p in (tmp_path / "control-plane").rglob("*") if p.is_file() and p.suffix != ".lock"}
    restored = recovery.RecoverableControl.restore(tmp_path, MODELS, "fixture-task")
    try:
        assert restored.get_usage() == expected_usage
        assert restored._handles[handle.node_id] == handle
        assert restored._submissions == control._submissions
        assert all(p.read_bytes() == value for p, value in saved.items())
        assert restored.cp.proj.work_items[handle.item_id].acceptance.value == "submitted"
        before = restored.cp.store.last_seq
        restored.record_call(handle, record)
        assert restored.cp.store.last_seq == before  # Idempotence survives restart.
        new = restored.start_role("verifier", "check")
        assert new.model == MODELS["verifier"]
    finally:
        restored.close()


@pytest.mark.parametrize("kind", ["identity", "manifest", "missing_call_commit", "partial_tail", "terminal"])
def test_ambiguous_cp_recovery_fails_closed(tmp_path, kind):
    control, handle, _ = control_fixture(tmp_path)
    if kind == "terminal":
        control.close()
    else:
        control.cp.close()
    directory = tmp_path / "control-plane"
    if kind == "manifest":
        Path(handle.manifest_path).write_text("{}")
    elif kind == "missing_call_commit":
        rich = directory / "rich-events.jsonl"
        rich.write_text(rich.read_text().splitlines()[0] + "\n")
    elif kind == "partial_tail":
        with (directory / "events.jsonl").open("a") as stream:
            stream.write('{"incomplete":')
    before = (directory / "events.jsonl").read_bytes()
    with pytest.raises(recovery.RecoveryRequired):
        recovery.RecoverableControl.restore(tmp_path, MODELS, "different" if kind == "identity" else "fixture-task")
    assert (directory / "events.jsonl").read_bytes() == before


def sandbox_fixture(root):
    for name in ("task", "workspace", "reports", "submission", "sandbox"):
        (root / name).mkdir()
    for name in ("brief.md", "spec.md"):
        (root / "task" / name).write_text("fixture")
    image = "sha256:" + "a" * 64
    prefix = "dpswarm-tb-abcdef123456"
    containers = {role: f"{i:064x}" for i, role in enumerate(recovery.ROLES, 1)}
    manifest = {"image": image, "prefix": prefix, "containers": containers,
                "task_dir": str(root / "task"), "run_dir": str(root),
                "closed": False, "freeze_started": False, "frozen": False, "frozen_roles": []}
    roles = []
    for role, cid in containers.items():
        mounts = [{"Type": "bind", "Source": str(root / "task/brief.md"), "Destination": "/task/brief.md", "RW": False}]
        if role != "executor":
            mounts.append({"Type": "bind", "Source": str(root / "task/spec.md"), "Destination": "/task/spec.md", "RW": False})
        if role != "planner":
            for directory in ("workspace", "reports"):
                mounts.append({"Type": "bind", "Source": str(root / directory), "Destination": "/shared/" + directory, "RW": role != "verifier"})
        if role in ("verifier", "oracle"):
            mounts.append({"Type": "bind", "Source": str(root / "submission"), "Destination": "/shared/submission", "RW": True})
        roles.append({"Id": cid, "Name": "/" + prefix + "-" + role, "Image": image,
                      "State": {"Running": True, "Paused": False, "Restarting": False, "Dead": False},
                      "Config": {"User": "10001:10001", "Labels": {"dpswarm.teambench.owner": prefix}},
                      "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True, "Privileged": False,
                                     "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges=true"]},
                      "Mounts": mounts})
    for filename, value in (("containers.json", manifest), ("image.inspect.json", [{"Id": image}]), ("roles.inspect.json", roles)):
        (root / "sandbox" / filename).write_text(json.dumps(value))
    return image, manifest, roles


def test_reattach_only_inspects_exact_saved_containers_without_writes(tmp_path, monkeypatch):
    image, manifest, roles = sandbox_fixture(tmp_path)
    commands = []
    def docker(args):
        commands.append(args)
        assert args[:3] == ["docker", "image", "inspect"] or args[:2] == ["docker", "inspect"]
        return SimpleNamespace(returncode=0, stdout=json.dumps([{"Id": image}] if args[1] == "image" else roles))
    monkeypatch.setattr(recovery, "_call", docker)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    sandbox = recovery.reattach_sandbox(tmp_path, image)
    assert sandbox.containers == manifest["containers"] and sandbox.prefix == manifest["prefix"]
    assert sandbox._closed is False and sandbox._freeze_started is False
    assert len(commands) == 2 and all(p.read_bytes() == b for p, b in before.items())


@pytest.mark.parametrize("change", ["owner", "paused", "image", "mount", "frozen", "missing_id"])
def test_reattach_rejects_changed_or_inactive_resources(tmp_path, monkeypatch, change):
    image, manifest, original = sandbox_fixture(tmp_path)
    roles = copy.deepcopy(original)
    if change == "owner": roles[0]["Config"]["Labels"]["dpswarm.teambench.owner"] = "other"
    elif change == "paused": roles[0]["State"]["Paused"] = True
    elif change == "image": roles[0]["Image"] = "sha256:" + "b" * 64
    elif change == "mount": roles[0]["Mounts"][0]["RW"] = True
    elif change == "missing_id": roles = roles[:-1]
    elif change == "frozen":
        manifest["freeze_started"] = True
        (tmp_path / "sandbox/containers.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(recovery, "_call", lambda args: SimpleNamespace(returncode=0,
        stdout=json.dumps([{"Id": image}] if args[1] == "image" else roles)))
    with pytest.raises(recovery.RecoveryRequired):
        recovery.reattach_sandbox(tmp_path, image)


def test_frozen_only_reattaches_for_cleanup_and_cannot_execute_tools(tmp_path, monkeypatch):
    image, manifest, roles = sandbox_fixture(tmp_path)
    manifest.update(freeze_started=True, frozen=True, frozen_roles=list(recovery.ROLES))
    (tmp_path / "sandbox/containers.json").write_text(json.dumps(manifest))
    for item in roles:
        item["State"]["Paused"] = True
    calls = []
    def docker(args):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=json.dumps([{"Id": image}] if args[1] == "image" else roles))
    monkeypatch.setattr(recovery, "_call", docker)
    with pytest.raises(recovery.RecoveryRequired):
        recovery.reattach_sandbox(tmp_path, image)
    sandbox = recovery.cleanup_finished_sandbox(tmp_path, image)
    assert sandbox._freeze_started is True
    assert sandbox.tool("executor", "run", {"cmd": "must not execute"})["exit_code"] == 1
    assert len(calls) == 2
    roles[0]["Config"]["Labels"]["dpswarm.teambench.owner"] = "wrong"
    with pytest.raises(recovery.RecoveryRequired):
        recovery.cleanup_finished_sandbox(tmp_path, image)


def test_already_closed_cleanup_does_not_call_docker(tmp_path, monkeypatch):
    image, manifest, _ = sandbox_fixture(tmp_path)
    manifest["closed"] = True
    (tmp_path / "sandbox/containers.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(recovery, "_call", lambda *a: pytest.fail("No Docker action expected"))
    assert recovery.cleanup_finished_sandbox(tmp_path, image) is None
    with pytest.raises(recovery.RecoveryRequired):
        recovery.cleanup_finished_sandbox(tmp_path, "wrong-image")


def test_final_cleanup_validates_recorded_stopped_grader_without_regrading(tmp_path, monkeypatch):
    image, manifest, roles = sandbox_fixture(tmp_path)
    (tmp_path / "grader").mkdir()
    grader = copy.deepcopy(roles[1])
    grader.update(Id="f" * 64, Name="/" + manifest["prefix"] + "-grader")
    grader["State"].update(Running=False, Paused=False)
    grader["Mounts"] = [{"Type": "bind", "Source": str(tmp_path / directory), "Destination": target, "RW": writable}
        for directory, target, writable in [("task", "/task", False), ("workspace", "/shared/workspace", True),
                                            ("reports", "/shared/reports", True), ("submission", "/shared/submission", False),
                                            ("grader", "/grader", False)]]
    manifest["containers"]["grader"] = grader["Id"]
    manifest.update(freeze_started=True, frozen=True, frozen_roles=list(recovery.ROLES))
    (tmp_path / "sandbox/containers.json").write_text(json.dumps(manifest))
    live = roles + [grader]
    monkeypatch.setattr(recovery, "_call", lambda args: SimpleNamespace(returncode=0,
        stdout=json.dumps([{"Id": image}] if args[1] == "image" else live)))
    sandbox = recovery.cleanup_finished_sandbox(tmp_path, image)
    assert sandbox.containers["grader"] == grader["Id"]
    assert sandbox._freeze_started is True
    grader["Mounts"][-1]["RW"] = True
    with pytest.raises(recovery.RecoveryRequired):
        recovery.cleanup_finished_sandbox(tmp_path, image)
