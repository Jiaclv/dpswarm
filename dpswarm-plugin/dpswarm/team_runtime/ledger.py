"""Durable runtime accounting, independent of CP acceptance and resource leases.

ExecutionStore has one owning writer. It detects stale writer instances rather
than merging their state. A running tool after recovery has an unknown outcome;
this module never executes or automatically retries it.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Callable


class LedgerError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"[{code}] {message}")


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def clone(value: Any) -> Any:
    return json.loads(canonical(value))


def nonnegative(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise LedgerError("INVALID_COUNT", f"{name} must be a nonnegative integer")
    return value


def identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LedgerError("INVALID_ID", f"{name} must be nonempty text")
    return value


def deadline(start: float, seconds: float | None) -> float | None:
    if seconds is None:
        return None
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds <= 0:
        raise LedgerError("INVALID_DEADLINE", "deadline_seconds must be positive and finite")
    return start + seconds


class RunBudget:
    """Reserve before each call; actual call IDs are bound once on completion.

    Inclusive input and output are added once. Cache/reasoning are dimensions,
    not extra charges. Unknown usage retains its reservation indefinitely.
    Reservations bound admission, not the provider's possible output overshoot.
    """

    def __init__(self, max_calls: int = 20, token_limit: int = 600_000,
                 deadline_seconds: float | None = None, *, clock: Callable[[], float] = time.time):
        self.max_calls = nonnegative(max_calls, "max_calls")
        self.token_limit = nonnegative(token_limit, "token_limit")
        self.clock = clock
        self.created_at = clock()
        self.deadline_at = deadline(self.created_at, deadline_seconds)
        self.tickets: dict[str, dict] = {}
        self.frozen = False
        self._lock = threading.RLock()

    def summary(self) -> dict:
        with self._lock:
            completed = [v for v in self.tickets.values() if v["status"] == "completed"]
            unknown = [v for v in completed if v["usage"]["total_tokens"] is None]
            pending = [v for v in self.tickets.values() if v["status"] == "reserved"]
            known = sum(v["usage"]["total_tokens"] for v in completed if v["usage"]["total_tokens"] is not None)
            held = sum(v["reserved_tokens"] for v in pending + unknown)
            return {"call_count": len(self.tickets), "completed_call_count": len(completed),
                    "pending_call_count": len(pending), "unknown_call_count": len(unknown),
                    "known_subtotal": known, "total_tokens": None if unknown or pending else known,
                    "reserved_tokens": held, "committed_tokens": known + held,
                    "remaining_calls": max(0, self.max_calls - len(self.tickets)),
                    "remaining_tokens": max(0, self.token_limit - known - held),
                    "over_token_limit": known + held > self.token_limit,
                    "deadline_at": self.deadline_at,
                    "deadline_exceeded": self.deadline_at is not None and self.clock() >= self.deadline_at,
                    "frozen": self.frozen}

    def reserve(self, ticket_id: str, role: str, reserved_tokens: int = 32768) -> dict:
        ticket_id, role = identifier(ticket_id, "ticket_id"), identifier(role, "role")
        nonnegative(reserved_tokens, "reserved_tokens")
        with self._lock:
            if self.frozen:
                raise LedgerError("FROZEN", "Budget no longer admits calls")
            existing = self.tickets.get(ticket_id)
            if existing:
                if existing["role"] != role or existing["reserved_tokens"] != reserved_tokens:
                    raise LedgerError("TICKET_CONFLICT", "Existing ticket has a different reservation")
                return deepcopy(existing)
            status = self.summary()
            if status["deadline_exceeded"]:
                raise LedgerError("DEADLINE_EXCEEDED", "Run deadline elapsed")
            if not status["remaining_calls"]:
                raise LedgerError("CALL_BUDGET_EXHAUSTED", "No model calls remain")
            if status["over_token_limit"] or reserved_tokens > status["remaining_tokens"]:
                raise LedgerError("TOKEN_BUDGET_EXHAUSTED", "Reservation exceeds remaining token budget")
            value = {"ticket_id": ticket_id, "role": role, "reserved_tokens": reserved_tokens,
                     "status": "reserved", "reserved_at": self.clock(), "call_id": None}
            self.tickets[ticket_id] = value
            return deepcopy(value)

    @staticmethod
    def _usage(record: dict) -> dict:
        fields = ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens", "total_tokens")
        values = {key: record.get(key) for key in fields}
        for key, value in values.items():
            if value is not None:
                nonnegative(value, key)
        inp, out = values["input_tokens"], values["output_tokens"]
        if inp is not None and values["cached_input_tokens"] is not None and values["cached_input_tokens"] > inp:
            raise LedgerError("INVALID_USAGE", "Cached input cannot exceed inclusive input")
        if out is not None and values["reasoning_tokens"] is not None and values["reasoning_tokens"] > out:
            raise LedgerError("INVALID_USAGE", "Reasoning cannot exceed inclusive output")
        reported = values["total_tokens"]
        total = inp + out if inp is not None and out is not None else reported
        return {**values, "reported_total_tokens": reported, "total_tokens": total,
                "total_source": "input_tokens + output_tokens" if inp is not None and out is not None
                                else ("reported_total_tokens" if reported is not None else None),
                "reported_total_mismatch": reported is not None and total != reported}

    def complete(self, ticket_id: str, record: dict) -> dict:
        with self._lock:
            if ticket_id not in self.tickets:
                raise LedgerError("UNKNOWN_TICKET", "Reserve a ticket before completing a call")
            if not isinstance(record, dict):
                raise LedgerError("INVALID_RECORD", "Call record must be an object")
            record = clone(record)
            call_id = identifier(record.get("call_id"), "call_id")
            ticket = self.tickets[ticket_id]
            if record.get("role") is not None and record["role"] != ticket["role"]:
                raise LedgerError("ROLE_MISMATCH", "Call role differs from its reservation")
            if ticket["status"] == "completed":
                if ticket["record_hash"] != digest(record):
                    raise LedgerError("COMPLETION_CONFLICT", "Ticket was already completed with different evidence")
                return deepcopy(ticket)
            if any(v.get("call_id") == call_id for key, v in self.tickets.items() if key != ticket_id):
                raise LedgerError("CALL_ID_REUSED", "An actual provider call cannot bind to two tickets")
            usage = self._usage(record)
            ticket.update(status="completed", completed_at=self.clock(), call_id=call_id,
                          record=record, record_hash=digest(record), usage=usage)
            return deepcopy(ticket)

    def freeze(self) -> None:
        with self._lock:
            self.frozen = True

    def snapshot(self) -> dict:
        with self._lock:
            value = {"version": 1, "max_calls": self.max_calls, "token_limit": self.token_limit,
                     "created_at": self.created_at, "deadline_at": self.deadline_at,
                     "frozen": self.frozen, "tickets": clone(self.tickets)}
            return {**value, "snapshot_hash": digest(value)}

    @classmethod
    def from_snapshot(cls, snapshot: dict, *, clock: Callable[[], float] = time.time) -> "RunBudget":
        value = clone(snapshot)
        saved_hash = value.pop("snapshot_hash", None)
        if saved_hash != digest(value) or value.get("version") != 1:
            raise LedgerError("SNAPSHOT_CORRUPT", "Budget snapshot hash or version is invalid")
        # Restoring evidence is not a new admission. Replaying reserve() would
        # reject expired runs or genuine provider overshoot, and dictionary
        # serialization need not preserve the original admission order.
        try:
            obj = cls(value["max_calls"], value["token_limit"], clock=clock)
            created, expires = value["created_at"], value["deadline_at"]
            for stamp in (created, expires):
                if stamp is not None and (isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp)):
                    raise LedgerError("SNAPSHOT_CORRUPT", "Invalid stored timestamp")
            if created is None or (expires is not None and expires <= created):
                raise LedgerError("SNAPSHOT_CORRUPT", "Invalid run deadline")
            tickets = value["tickets"]
            if not isinstance(tickets, dict) or len(tickets) > obj.max_calls or not isinstance(value["frozen"], bool):
                raise LedgerError("SNAPSHOT_CORRUPT", "Invalid ticket count or frozen state")
            call_ids = set()
            for key, ticket in tickets.items():
                identifier(key, "ticket_id")
                identifier(ticket["role"], "role")
                nonnegative(ticket["reserved_tokens"], "reserved_tokens")
                if key != ticket["ticket_id"] or ticket["reserved_tokens"] > obj.token_limit:
                    raise LedgerError("SNAPSHOT_CORRUPT", "Invalid ticket identity or reservation")
                if ticket["status"] == "completed":
                    record = ticket["record"]
                    if not isinstance(record, dict):
                        raise LedgerError("SNAPSHOT_CORRUPT", "Invalid call record")
                    call_id = identifier(record.get("call_id"), "call_id")
                    if call_id in call_ids or call_id != ticket["call_id"]:
                        raise LedgerError("SNAPSHOT_CORRUPT", "Actual call ID binding is not unique")
                    if record.get("role") is not None and record["role"] != ticket["role"]:
                        raise LedgerError("SNAPSHOT_CORRUPT", "Call role differs from reservation")
                    if cls._usage(record) != ticket["usage"] or digest(record) != ticket["record_hash"]:
                        raise LedgerError("SNAPSHOT_CORRUPT", "Ticket usage differs from call evidence")
                    call_ids.add(call_id)
                elif ticket["status"] != "reserved" or ticket["call_id"] is not None:
                    raise LedgerError("SNAPSHOT_CORRUPT", "Invalid reserved ticket state")
            obj.created_at, obj.deadline_at = created, expires
            obj.tickets, obj.frozen = tickets, value["frozen"]
        except (KeyError, TypeError, AttributeError) as exc:
            raise LedgerError("SNAPSHOT_CORRUPT", str(exc)) from exc
        return obj


class ExecutionStore:
    """Hash-chained single-writer journal plus atomic checkpoint acceleration."""

    def __init__(self, directory: Path, *, clock: Callable[[], float] = time.time):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.journal = self.directory / "execution.jsonl"
        self.snapshot_path = self.directory / "execution.snapshot.json"
        self.clock = clock
        self._lock = threading.RLock()
        self._events = self._read_events()
        self._tools: dict[str, dict] = {}
        for event in self._events:
            self._apply_tool(event)

    def _read_events(self) -> list[dict]:
        if not self.journal.exists():
            return []
        raw = self.journal.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raise LedgerError("JOURNAL_TRUNCATED", "Incomplete journal tail requires explicit recovery")
        events, previous = [], "0" * 64
        try:
            for seq, line in enumerate(raw.splitlines(), 1):
                event = json.loads(line)
                value = {k: v for k, v in event.items() if k != "hash"}
                if event["seq"] != seq or event["prev_hash"] != previous or event["hash"] != digest(value):
                    raise LedgerError("JOURNAL_CORRUPT", "Journal sequence or hash chain mismatch")
                identifier(event["kind"], "event kind")
                if not isinstance(event["payload"], dict):
                    raise LedgerError("JOURNAL_CORRUPT", "Event payload must be an object")
                events.append(event)
                previous = event["hash"]
        except (KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise LedgerError("JOURNAL_CORRUPT", str(exc)) from exc
        return events

    def _assert_current(self) -> None:
        current = self._read_events()
        if len(current) != len(self._events) or (current and current[-1]["hash"] != self._events[-1]["hash"]):
            raise LedgerError("STALE_WRITER", "Another writer changed this journal; reopen and reconcile")

    def append(self, kind: str, payload: dict) -> dict:
        identifier(kind, "event kind")
        if not isinstance(payload, dict):
            raise LedgerError("INVALID_EVENT", "Event payload must be an object")
        with self._lock:
            self._assert_current()
            value = {"seq": len(self._events) + 1, "at": self.clock(), "kind": kind,
                     "payload": clone(payload), "prev_hash": self._events[-1]["hash"] if self._events else "0" * 64}
            event = {**value, "hash": digest(value)}
            # Validate tool transitions before the event becomes durable.
            previous = deepcopy(self._tools)
            try:
                self._apply_tool(event)
                with self.journal.open("ab") as stream:
                    stream.write((canonical(event) + "\n").encode("utf-8"))
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                self._tools = previous
                raise
            self._events.append(event)
            return deepcopy(event)

    def events_since(self, seq: int = 0) -> list[dict]:
        with self._lock:
            nonnegative(seq, "seq")
            if seq > len(self._events):
                raise LedgerError("INVALID_SEQUENCE", "Sequence is beyond the journal head")
            return deepcopy(self._events[seq:])

    def _apply_tool(self, event: dict) -> None:
        kind, value = event["kind"], event["payload"]
        if kind not in ("tool.planned", "tool.running", "tool.completed"):
            return
        key = identifier(value.get("operation_id"), "operation_id")
        existing = self._tools.get(key)
        if kind == "tool.planned":
            if existing or digest(value["request"]) != value["request_hash"]:
                raise LedgerError("TOOL_CONFLICT", "Invalid or duplicate tool plan")
            self._tools[key] = {**clone(value), "status": "planned", "planned_seq": event["seq"]}
        elif kind == "tool.running":
            if not existing or existing["status"] != "planned":
                raise LedgerError("TOOL_STATE", "Only a planned tool may start")
            existing.update(status="running", running_seq=event["seq"])
        else:
            if not existing or existing["status"] != "running" or digest(value["result"]) != value["result_hash"]:
                raise LedgerError("TOOL_STATE", "Only a running tool may complete with valid evidence")
            existing.update(status="completed", result=clone(value["result"]),
                            result_hash=value["result_hash"], completed_seq=event["seq"])

    def plan_tool(self, operation_id: str, request: dict) -> dict:
        identifier(operation_id, "operation_id")
        if not isinstance(request, dict):
            raise LedgerError("INVALID_REQUEST", "Tool request must be an object")
        with self._lock:
            self._assert_current()
            old = self._tools.get(operation_id)
            if old:
                if old["request_hash"] != digest(request):
                    raise LedgerError("TOOL_CONFLICT", "Operation ID already has a different request")
                return deepcopy(old)
            self.append("tool.planned", {"operation_id": operation_id, "request": request, "request_hash": digest(request)})
            return self.tool_state(operation_id)

    def start_tool(self, operation_id: str) -> dict:
        with self._lock:
            old = self.tool_state(operation_id)
            if old is None:
                raise LedgerError("UNKNOWN_TOOL", "Plan a tool before starting it")
            if old["status"] == "running":
                raise LedgerError("TOOL_OUTCOME_UNKNOWN", "Tool may already have run; automatic replay is forbidden")
            if old["status"] == "completed":
                raise LedgerError("TOOL_ALREADY_COMPLETED", "Use replay_completed() instead of executing again")
            self.append("tool.running", {"operation_id": operation_id})
            return self.tool_state(operation_id)

    def complete_tool(self, operation_id: str, result: Any) -> dict:
        with self._lock:
            self._assert_current()
            old = self.tool_state(operation_id)
            if old and old["status"] == "completed":
                if old["result_hash"] != digest(result):
                    raise LedgerError("TOOL_CONFLICT", "Tool completed with different evidence")
                return old
            self.append("tool.completed", {"operation_id": operation_id, "result": result, "result_hash": digest(result)})
            return self.tool_state(operation_id)

    def tool_state(self, operation_id: str) -> dict | None:
        with self._lock:
            return deepcopy(self._tools.get(operation_id))

    def replay_completed(self, operation_id: str) -> Any:
        value = self.tool_state(operation_id)
        if not value or value["status"] != "completed":
            raise LedgerError("TOOL_OUTCOME_UNKNOWN", "No completed tool result is available")
        return deepcopy(value["result"])

    def save_snapshot(self, state: dict) -> dict:
        if not isinstance(state, dict):
            raise LedgerError("INVALID_SNAPSHOT", "Execution state must be an object")
        with self._lock:
            event = self.append("execution.checkpoint", {"state": state})
            value = {"version": 1, "event_seq": event["seq"], "event_hash": event["hash"], "state": clone(state)}
            envelope = {**value, "snapshot_hash": digest(value)}
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="wb", dir=self.directory, prefix=".checkpoint-", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write((canonical(envelope) + "\n").encode("utf-8"))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.snapshot_path)
            finally:
                if temporary is not None and temporary.exists():
                    temporary.unlink()
            return clone(envelope)

    def load_snapshot(self) -> dict | None:
        with self._lock:
            self._assert_current()
            if self.snapshot_path.exists():
                try:
                    value = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
                    saved_hash = value.pop("snapshot_hash", None)
                    seq = value["event_seq"]
                    if saved_hash != digest(value) or value["version"] != 1 or not 1 <= seq <= len(self._events):
                        raise LedgerError("SNAPSHOT_CORRUPT", "Execution snapshot hash or sequence mismatch")
                    event = self._events[seq - 1]
                    if event["hash"] != value["event_hash"] or event["kind"] != "execution.checkpoint" or event["payload"]["state"] != value["state"]:
                        raise LedgerError("SNAPSHOT_CORRUPT", "Snapshot does not match its journal checkpoint")
                except (KeyError, TypeError, json.JSONDecodeError) as exc:
                    raise LedgerError("SNAPSHOT_CORRUPT", str(exc)) from exc
            checkpoints = [event for event in self._events if event["kind"] == "execution.checkpoint"]
            if not checkpoints:
                return None
            latest = checkpoints[-1]
            return {"state": clone(latest["payload"]["state"]), "event_seq": latest["seq"],
                    "event_hash": latest["hash"], "pending_events": self.events_since(latest["seq"])}
