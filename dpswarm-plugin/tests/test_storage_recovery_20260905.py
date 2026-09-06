"""Offline regressions for acknowledged event recovery and writer exclusion."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from dpswarm.events import Event, EventStore
from dpswarm.team_runtime.ledger import ExecutionStore, LedgerError


@pytest.mark.parametrize("tail", [
    b'{"txn": 99, "events": [',
    b'{"txn": 99, "events": [{"payload": "' + "中".encode("utf-8")[:2],
])
def test_event_tail_recovery_repairs_bytes_before_acknowledging_new_write(tmp_path, tail):
    path = tmp_path / "events.jsonl"
    first = EventStore(path)
    try:
        first.append("root_started", {"text": "合法中文前缀"})
    finally:
        first.close()
    valid_prefix = path.read_bytes()
    path.write_bytes(valid_prefix + tail)
    recovered = EventStore(path)
    try:
        assert path.read_bytes() == valid_prefix
        events = [Event(1, "spec_published", {"fixture": "new"}),
                  Event(2, "work_item_created", {"fixture": "same transaction"})]
        assert recovered.append_txn(events) == events
    finally:
        recovered.close()
    reopened = EventStore(path)
    try:
        assert [event.seq for event in reopened.read_all()] == [0, 1, 2]
        assert reopened.read_all()[0].payload["text"] == "合法中文前缀"
        assert len(path.read_bytes().splitlines()) == 2
    finally:
        reopened.close()


@pytest.mark.parametrize("legacy", [False, True])
def test_complete_event_without_final_newline_remains_appendable(tmp_path, legacy):
    path = tmp_path / "events.jsonl"
    event = Event(0, "root_started", {"text": "中文"}).to_dict()
    value = event if legacy else {"txn": 1, "events": [event]}
    path.write_bytes(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    store = EventStore(path)
    try:
        assert store.last_seq == 0
        assert path.read_bytes().endswith(b"\n")
        store.append("spec_published", {"fixture": "after complete tail"})
    finally:
        store.close()
    store = EventStore(path)
    try:
        assert store.last_seq == 1
    finally:
        store.close()


@pytest.mark.parametrize("corruption", [b"{broken}\n", b'\xff\n'])
@pytest.mark.parametrize("is_middle", [False, True])
def test_complete_line_corruption_fails_closed_without_modifying_file(tmp_path, corruption, is_middle):
    path = tmp_path / "events.jsonl"
    event = json.dumps(Event(0, "root_started").to_dict()).encode() + b"\n"
    raw = corruption + event if is_middle else event + corruption
    path.write_bytes(raw)
    with pytest.raises(RuntimeError, match="EVENT_LOG_CORRUPT"):
        EventStore(path)
    assert path.read_bytes() == raw


def test_invalid_prefix_is_not_repaired_even_with_incomplete_tail_and_lock_is_released(tmp_path):
    path = tmp_path / "events.jsonl"
    bad_event = Event(7, "root_started").to_dict()
    raw = json.dumps(bad_event).encode() + b'\n{"txn": 2'
    path.write_bytes(raw)
    with pytest.raises(RuntimeError, match="EVENT_LOG_CORRUPT"):
        EventStore(path)
    assert path.read_bytes() == raw
    path.write_bytes(b"")
    store = EventStore(path)
    try:
        store.append("root_started", {})
    finally:
        store.close()


def test_closed_event_writer_cannot_append_after_lock_transfers(tmp_path):
    path = tmp_path / "events.jsonl"
    first = EventStore(path)
    first.append("root_started", {})
    first.close()
    second = EventStore(path)
    try:
        with pytest.raises(RuntimeError, match="EVENT_LOG_CLOSED"):
            first.append("spec_published", {"stale": True})
        assert second.append("spec_published", {"valid": True}).seq == 1
    finally:
        second.close()
    assert path.with_suffix(".jsonl.lock").exists()


def test_execution_append_excludes_other_instance_before_staleness_check(tmp_path, monkeypatch):
    first, second = ExecutionStore(tmp_path), ExecutionStore(tmp_path)
    checked, finish = threading.Event(), threading.Event()
    original = first._assert_current

    def pause_after_check():
        original()
        checked.set()
        assert finish.wait(10), "test did not release writer"

    monkeypatch.setattr(first, "_assert_current", pause_after_check)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(first.append, "fixture", {"owner": 1})
        try:
            assert checked.wait(5), "first writer did not reach freshness check"
            with pytest.raises(LedgerError) as error:
                second.append("fixture", {"owner": 2})
            assert error.value.code == "WRITER_BUSY"
        finally:
            finish.set()
        assert pending.result(timeout=5)["seq"] == 1
    assert len(ExecutionStore(tmp_path).events_since()) == 1
    with pytest.raises(LedgerError) as error:
        second.append("fixture", {"owner": 2})
    assert error.value.code == "STALE_WRITER"


def test_execution_lock_is_held_until_atomic_snapshot_replacement(tmp_path, monkeypatch):
    first, second = ExecutionStore(tmp_path), ExecutionStore(tmp_path)
    import dpswarm.team_runtime.ledger as ledger
    original_replace = ledger.os.replace
    rejected = []

    def inspect_before_replace(source, target):
        with pytest.raises(LedgerError) as error:
            second.append("fixture", {"owner": 2})
        rejected.append(error.value.code)
        return original_replace(source, target)

    monkeypatch.setattr(ledger.os, "replace", inspect_before_replace)
    first.save_snapshot({"phase": "durable"})
    assert rejected == ["WRITER_BUSY"]
    assert ExecutionStore(tmp_path).load_snapshot()["state"] == {"phase": "durable"}


def test_execution_lock_releases_after_append_validation_error(tmp_path):
    store = ExecutionStore(tmp_path)
    with pytest.raises(LedgerError, match="TOOL_STATE"):
        store.append("tool.running", {"operation_id": "unplanned"})
    assert ExecutionStore(tmp_path).append("fixture", {"valid": True})["seq"] == 1


def test_execution_lock_releases_after_snapshot_replace_failure(tmp_path, monkeypatch):
    import dpswarm.team_runtime.ledger as ledger
    store = ExecutionStore(tmp_path)

    def fail_replace(*args):
        raise PermissionError("injected snapshot replacement failure")

    monkeypatch.setattr(ledger.os, "replace", fail_replace)
    with pytest.raises(PermissionError, match="injected"):
        store.save_snapshot({"phase": "journal-durable"})
    recovered = ExecutionStore(tmp_path)
    assert recovered.load_snapshot()["state"] == {"phase": "journal-durable"}
    assert recovered.append("fixture", {})["seq"] == 2
    assert list(tmp_path.glob(".checkpoint-*")) == []


CHILD_WRITER = """
from pathlib import Path
import sys
from dpswarm.team_runtime.ledger import ExecutionStore
class PausedStore(ExecutionStore):
    def _assert_current(self):
        super()._assert_current()
        print('checked', flush=True)
        if sys.stdin.readline().strip() != 'continue':
            raise RuntimeError('test writer was not released')
store = PausedStore(Path(sys.argv[1]))
store.append('fixture', {'owner': 'child'})
print('committed', flush=True)
"""


def test_execution_cross_process_check_and_append_are_one_critical_section(tmp_path):
    second = ExecutionStore(tmp_path)
    child = subprocess.Popen([sys.executable, "-u", "-c", CHILD_WRITER, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            ready = executor.submit(child.stdout.readline)
            try:
                assert ready.result(timeout=10).strip() == "checked"
            except BaseException:
                child.kill()
                raise
        with pytest.raises(LedgerError) as error:
            second.append("fixture", {"owner": "parent"})
        assert error.value.code == "WRITER_BUSY"
        output, errors = child.communicate("continue\n", timeout=10)
        assert child.returncode == 0, errors
        assert "committed" in output
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)
    assert len(ExecutionStore(tmp_path).events_since()) == 1


def test_execution_process_exit_releases_lock_without_deleting_lock_file(tmp_path):
    child = subprocess.Popen([sys.executable, "-u", "-c", CHILD_WRITER, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            ready = executor.submit(child.stdout.readline)
            try:
                assert ready.result(timeout=10).strip() == "checked"
            except BaseException:
                child.kill()
                raise
    finally:
        child.kill()
        child.communicate(timeout=10)
    assert (tmp_path / "execution.jsonl.lock").exists()
    assert ExecutionStore(tmp_path).append("fixture", {"owner": "survivor"})["seq"] == 1


def test_event_partial_write_requires_recovery_before_any_later_append(tmp_path, monkeypatch):
    import builtins
    import dpswarm.events as events_module
    path = tmp_path / "events.jsonl"
    store = EventStore(path)
    store.append("root_started", {})
    prefix = path.read_bytes()
    original_open = builtins.open

    class PartialWriter:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def write(self, data):
            self.stream.write(data[:15])
            self.stream.flush()
            raise OSError("injected partial write")

    def faulty_open(file, mode="r", *args, **kwargs):
        stream = original_open(file, mode, *args, **kwargs)
        return PartialWriter(stream) if Path(file) == path and mode == "ab" else stream

    try:
        with monkeypatch.context() as patch:
            patch.setattr(events_module, "open", faulty_open, raising=False)
            with pytest.raises(OSError, match="partial write"):
                store.append("spec_published", {"unacknowledged": True})
        assert store.last_seq == 0
        assert store._txn_id == 1
        damaged = path.read_bytes()
        assert damaged.startswith(prefix) and damaged != prefix
        with pytest.raises(RuntimeError, match="EVENT_LOG_WRITE_FAILED"):
            store.append("spec_published", {"would_hide_partial": True})
        assert path.read_bytes() == damaged
    finally:
        store.close()
    restored = EventStore(path)
    try:
        assert path.read_bytes() == prefix
        assert restored.append("spec_published", {"recovered": True}).seq == 1
    finally:
        restored.close()
    restored = EventStore(path)
    try:
        assert restored.last_seq == 1
    finally:
        restored.close()


def test_event_fsync_failure_preserves_unknown_outcome_and_blocks_followup(tmp_path, monkeypatch):
    import dpswarm.events as events_module
    path = tmp_path / "events.jsonl"
    store = EventStore(path)

    def fail_sync(descriptor):
        raise OSError("injected uncertain fsync")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(events_module.os, "fsync", fail_sync)
            with pytest.raises(OSError, match="uncertain fsync"):
                store.append("root_started", {"unknown_durability": True})
        assert store.last_seq == -1
        assert store._txn_id == 0
        with pytest.raises(RuntimeError, match="EVENT_LOG_WRITE_FAILED"):
            store.append("root_started", {"must_not_duplicate": True})
    finally:
        store.close()
    # The write happened, but no acknowledgement was returned. Recovery decides
    # the actual head; a retry cannot silently duplicate its sequence.
    restored = EventStore(path)
    try:
        assert restored.last_seq == 0
        assert restored.append("spec_published", {}).seq == 1
    finally:
        restored.close()


def test_event_repair_fsync_failure_releases_lifetime_lock(tmp_path, monkeypatch):
    import dpswarm.events as events_module
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"incomplete":')

    def fail_sync(descriptor):
        raise OSError("injected repair fsync")

    with monkeypatch.context() as patch:
        patch.setattr(events_module.os, "fsync", fail_sync)
        with pytest.raises(OSError, match="repair fsync"):
            EventStore(path)
    store = EventStore(path)
    try:
        assert store.append("root_started", {}).seq == 0
    finally:
        store.close()
