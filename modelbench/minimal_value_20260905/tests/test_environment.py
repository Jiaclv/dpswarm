import hashlib
import json
import subprocess

import pytest

from modelbench.minimal_value_20260905 import environment as module


INSTANCE = {"instance_id": "org__repo-42", "repo": "org/repo", "base_commit": "a" * 40,
            "problem_statement": "Public task", "version": "1"}


class DockerState:
    def __init__(self, env):
        self.env = env
        self.running = True
        self.exists = True
        self.owner = env.owner
        self.calls = []
        self.stop_sticks = True
        self.remove_sticks = True
        self.restart_fails = False
        self.exec_code = 0

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        code, stdout, stderr = 0, "", ""
        if args[0] == "inspect":
            if self.exists:
                stdout = json.dumps([{"Config": {"Labels": {"dpswarm.swe.owner": self.owner}},
                                      "State": {"Running": self.running, "Pid": 321 if self.running else 0}}])
            else:
                code, stderr = 1, "Error: No such object: candidate-id"
        elif args[0] == "stop" and self.stop_sticks:
            self.running = False
        elif args[0] == "start":
            if self.restart_fails:
                raise module.EnvironmentError("restart failed")
            self.running = True
        elif args[0] == "rm" and self.remove_sticks:
            self.exists = False
        elif args[0] == "exec":
            request = json.loads(kwargs["input"])
            code = self.exec_code
            stdout = "frozen patch\n" if request["operation"] == "export" else ""
        return subprocess.CompletedProcess(args, code, stdout, stderr)


@pytest.fixture
def ready(tmp_path, monkeypatch):
    env = module.ValueEnvironment(INSTANCE, tmp_path / "env")
    env.container_id = "candidate-id"
    state = DockerState(env)
    monkeypatch.setattr(module, "_docker", state)
    return env, state


def test_terminal_export_requires_confirmed_stop_and_permanent_candidate_fence(ready):
    env, state = ready
    with pytest.raises(module.EnvironmentError, match="quiesce"):
        env.export_patch()
    evidence = env.quiesce()
    assert evidence["stopped_pid"] == 0 and evidence["maintenance_started"]
    assert env.export_patch() == "frozen patch\n"
    for action in (lambda: env.run("touch /testbed/late"), lambda: env.apply_patch("patch")):
        with pytest.raises(module.EnvironmentError, match="after quiesce"):
            action()
    operations = [args[0] for args, _ in state.calls]
    assert operations.index("stop") < operations.index("start") < operations.index("exec")
    assert env.quiesce() == evidence


def test_failed_stop_cannot_produce_frozen_artifact(ready):
    env, state = ready
    state.stop_sticks = False
    with pytest.raises(module.EnvironmentError, match="confirmed stopped"):
        env.quiesce()
    with pytest.raises(module.EnvironmentError, match="quiesce"):
        env.export_patch()
    assert "exec" not in [args[0] for args, _ in state.calls]


def test_restart_failure_stays_fenced_and_has_no_export(ready):
    env, state = ready
    state.restart_fails = True
    with pytest.raises(module.EnvironmentError, match="restart failed"):
        env.quiesce()
    with pytest.raises(module.EnvironmentError, match="after quiesce"):
        env.run("echo unsafe")
    with pytest.raises(module.EnvironmentError, match="quiesce"):
        env.export_patch()


def test_ownership_mismatch_never_stops_or_removes_other_container(ready):
    env, state = ready
    state.owner = "another-owner"
    with pytest.raises(module.EnvironmentError, match="ownership"):
        env.quiesce()
    with pytest.raises(module.EnvironmentError, match="ownership"):
        env.close()
    assert all(args[0] == "inspect" for args, _ in state.calls)


def test_cleanup_requires_confirmed_absence_and_can_be_reconciled(ready):
    env, state = ready
    state.remove_sticks = False
    with pytest.raises(module.EnvironmentError, match="not confirmed"):
        env.close()
    assert env.container_id and not env._closed
    record = json.loads((env.run_dir / "cleanup.json").read_text())
    assert not record["closed"] and record["errors"]
    state.remove_sticks = True
    result = env.close()
    assert result["closed"] and result["removed"]
    assert env.close() == result


def test_applicability_is_bound_to_original_base_and_exact_patch(ready):
    env, state = ready
    env.quiesce()
    patch = "some patch\n"
    result = env.check_frozen_patch(patch)
    assert result["applicable"] is True
    assert result["baseline_commit"] == INSTANCE["base_commit"]
    assert result["patch_sha256"] == hashlib.sha256(patch.encode()).hexdigest()
    request = json.loads(state.calls[-1][1]["input"])
    assert request["baseline"] == INSTANCE["base_commit"] and request["patch"] == patch
    state.exec_code = 1
    assert env.check_frozen_patch(patch)["applicable"] is False


def test_fork_and_clone_keep_adapter_contract(ready, tmp_path):
    env, _ = ready
    for child in (env.fork(tmp_path / "child", baseline_patch="delta"), env.clone(tmp_path / "clone")):
        assert isinstance(child, module.ValueEnvironment)
        assert child.owner != env.owner and child.container_id is None
    with pytest.raises(module.EnvironmentError, match="host rootgrade_terminal"):
        env.grade("patch")


def test_terminal_grader_rejects_changed_bytes_before_any_backend(tmp_path, monkeypatch):
    path = tmp_path / "candidate.patch"
    path.write_bytes(b"changed")
    monkeypatch.setattr(module, "_verify_adapter", lambda _: pytest.fail("should not inspect grader after hash mismatch"))
    with pytest.raises(module.EnvironmentError, match="hash mismatch"):
        module.rootgrade_terminal(INSTANCE, path, "0" * 64, tmp_path / "grader", grader_contract={})

def test_owner_intent_is_durable_before_container_creation(tmp_path, monkeypatch):
    env = module.ValueEnvironment(INSTANCE, tmp_path / "intent")
    def crash_before_create(self):
        intent = json.loads((self.run_dir / "container-intent.json").read_text())
        assert intent["owner"] == self.owner
        assert intent["name"] == "dpswarm-swe-" + self.owner[:16]
        raise RuntimeError("simulated create-record crash")
    monkeypatch.setattr(module.legacy.SWEEnvironment, "start", crash_before_create)
    with pytest.raises(RuntimeError, match="create-record crash"):
        env.start()
    assert (env.run_dir / "container-intent.json").exists()
