"""Pure fake Docker tests: never change a real container."""
from datetime import datetime, timezone, timedelta
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "memory_limit_enforcer.py"
spec = importlib.util.spec_from_file_location("memory_enforcer_fixture", SOURCE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
NOW = datetime(2026, 9, 5, 5, tzinfo=timezone.utc)
A, B = "a"*32, "b"*32
CID, OTHER = "1"*64, "2"*64


def reply(code=0, value=None, error=""):
    return SimpleNamespace(returncode=code, stdout=json.dumps(value) if value is not None else "", stderr=error)


class FakeDocker:
    def __init__(self, *, memory=3*m.GIB, swap=3*m.GIB, owner=A):
        self.containers = {CID: {"Id": CID, "Config": {"Labels": {"dpswarm.swe.owner": owner}},
                                "HostConfig": {"Memory": memory, "MemorySwap": swap},
                                "State": {"Pid": 101, "OOMKilled": False}, "Created": "now"}}
        self.calls, self.fail_update, self.disappear = [], False, False

    def __call__(self, args, *, timeout=8):
        self.calls.append(list(args))
        if args[0] == "ps":
            owner = args[-1].split("=")[-1]
            ids = [identity for identity, row in self.containers.items()
                   if row["Config"]["Labels"]["dpswarm.swe.owner"] == owner]
            return SimpleNamespace(returncode=0, stdout="\n".join(ids), stderr="")
        if args[0] == "inspect":
            if args[1] not in self.containers:
                return reply(1, error="Error: No such object")
            return reply(value=[self.containers[args[1]]])
        assert args[0] == "update"
        if self.disappear:
            self.containers.pop(args[-1], None)
            return reply(1, error="No such container")
        if self.fail_update:
            return reply(1, error="update denied")
        self.containers[args[-1]]["HostConfig"].update(Memory=int(args[2]), MemorySwap=int(args[4]))
        return reply()


def test_owned_three_gib_is_lowered_and_both_observed_values_are_verified():
    docker = FakeDocker()
    result = m.apply_cap(CID, A, [{"path": "intent", "sha256": "frozen"}], docker=docker, now=lambda: NOW)
    assert result["status"] == "updated_and_verified"
    assert result["requested_previous_memory_bytes"] == 3*m.GIB
    assert result["observed_memory_bytes"] == result["observed_swap_bytes"] == m.GIB
    assert result["container_pid"] == 101 and result["enforcer_sha256"] == m.sha(SOURCE)
    assert docker.calls[1] == ["update", "--memory", str(m.GIB), "--memory-swap", str(m.GIB), CID]


def test_existing_768_mib_helper_is_preserved_without_increasing_its_limit():
    limit = 768*1024**2
    docker = FakeDocker(memory=limit, swap=limit)
    result = m.apply_cap(CID, A, [], docker=docker)
    assert result["status"] == "already_within_cap"
    assert result["observed_memory_bytes"] == limit
    assert not any(call[0] == "update" for call in docker.calls)


def test_unlimited_memory_is_bounded():
    docker = FakeDocker(memory=0, swap=-1)
    result = m.apply_cap(CID, A, [], docker=docker)
    assert result["status"] == "updated_and_verified"
    assert result["observed_memory_bytes"] == result["observed_swap_bytes"] == m.GIB


def test_foreign_label_never_reaches_docker_update():
    docker = FakeDocker(owner=B)
    with pytest.raises(m.EnforcementError, match="owner label"):
        m.apply_cap(CID, A, [], docker=docker)
    assert not any(call[0] == "update" for call in docker.calls)


def test_failed_update_cannot_claim_verified_success():
    docker = FakeDocker()
    docker.fail_update = True
    result = m.apply_cap(CID, A, [], docker=docker)
    assert result["status"] == "enforcement_failed"
    assert result["observed_memory_bytes"] == 3*m.GIB


def test_removed_container_is_reported_without_claiming_verified_update():
    docker = FakeDocker()
    docker.disappear = True
    result = m.apply_cap(CID, A, [], docker=docker)
    assert result["status"] == "disappeared_during_update"


def test_registration_scope_includes_intents_and_later_grader_request_only(tmp_path):
    roots = [tmp_path / "episode"]
    m.write(roots[0] / "environment/container-intent.json", {"owner": A, "name": "candidate"})
    m.write(roots[0] / "grade-1/grader/request.json", {"owner": B, "grader_contract": {"frozen": True}, "memory": "3g"})
    m.write(tmp_path / "foreign/container.json", {"owner": "c"*32, "id": OTHER})
    registered = m.registrations(roots)
    assert set(registered) == {A, B}
    assert all(item["sha256"] for values in registered.values() for item in values)
    docker = FakeDocker()
    docker.containers[OTHER] = {"Id": OTHER, "Config": {"Labels": {"dpswarm.swe.owner": "c"*32}}}
    assert m.discover(registered, docker) == {CID: A}
    assert all("c"*32 not in call[-1] for call in docker.calls)


@pytest.fixture
def group(tmp_path):
    batch, directory = tmp_path / "batch", tmp_path / "group"
    ids = ["e1", "e2", "e3", "e4"]
    m.write(batch / "manifest.json", {"schedule": [{"run_id": identity} for identity in ids]})
    m.write(directory / "group.json", {"batch": str(batch), "run_ids": ids,
                                      "manifest_sha256": m.sha(batch / "manifest.json")})
    m.write(directory / "controller-launch/launch.json", {"started_at": NOW.isoformat(), "pid": 123})
    m.write(batch / "results/e1/environment/container-intent.json", {"owner": A, "name": "candidate"})
    plan = m.group_contract(directory) | {"enforcer_sha256": m.sha(SOURCE)}
    m.write(directory / "memory-1g-enforcer/plan.json", plan)
    return directory, batch, plan


def test_future_grader_owner_is_discovered_then_group_exits_only_when_absent(group):
    directory, batch, plan = group
    docker = FakeDocker()
    ticks = [0]
    def sleep(seconds):
        assert seconds == .5
        ticks[0] += 1
        if ticks[0] == 1:
            docker.containers.clear()
            docker.containers[OTHER] = {
                "Id": OTHER, "Config": {"Labels": {"dpswarm.swe.owner": B}},
                "HostConfig": {"Memory": 3*m.GIB, "MemorySwap": 3*m.GIB}, "State": {}}
            m.write(batch / "results/e4/grade-1/grader/request.json",
                    {"owner": B, "grader_contract": {"version": 1}, "memory": "3g"})
            m.write(directory / "trial-state.json", {"status": "completed_awaiting_review"})
        elif ticks[0] == 2:
            docker.containers.clear()
        else:
            pytest.fail("helper did not exit after terminal group and absent owned containers")
    result = m.run(directory, docker=docker, now=lambda: NOW + timedelta(minutes=1), sleep=sleep)
    assert result["status"] == "group_finished_owned_containers_absent"
    assert result["verified_update_count"] == 2 and ticks[0] == 2
    assert len([call for call in docker.calls if call[0] == "update"]) == 2
    assert m.read(batch / "results/e4/grade-1/grader/request.json")["memory"] == "3g"


def test_failure_alarm_is_durable_and_runtime_policy_remains_untouched(group):
    directory, batch, plan = group
    docker = FakeDocker()
    docker.fail_update = True
    before = (directory / "group.json").read_bytes()
    result = m.run(directory, docker=docker, now=lambda: NOW, sleep=lambda _: pytest.fail("must fail explicitly"))
    assert result["status"] == "failed"
    assert m.read(directory / "memory-1g-enforcer/ALARM.json")["status"] == "enforcement_failed"
    assert (directory / "group.json").read_bytes() == before


def test_deadline_is_original_group_start_plus_9300_not_new_helper_window(group):
    directory, batch, plan = group
    docker = FakeDocker()
    assert plan["deadline"] == (NOW + timedelta(seconds=9300)).isoformat()
    result = m.run(directory, docker=docker, now=lambda: NOW + timedelta(seconds=9301))
    assert result["status"] == "group_watchdog_deadline"
    assert not docker.calls
