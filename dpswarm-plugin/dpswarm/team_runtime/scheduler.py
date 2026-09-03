"""Explicit, bounded clarification state machine; no model or CP side effects.

The runner owns physical scheduling, history, tool-batch boundaries and CP
admission. Waiting here neither releases a CP lease nor changes acceptance.
"""
from __future__ import annotations

from copy import deepcopy
import threading
import time
from typing import Callable

from .ledger import LedgerError, clone, deadline, digest, identifier, nonnegative


class ClarificationError(LedgerError):
    pass


CONTEXT_KEYS = ("run_id", "item_id", "attempt", "node_id", "session_id", "context_epoch")
PENDING_STATES = {"E_WAITING_REPLY", "ADMITTED", "P_REPLYING"}


def context(value: dict) -> dict:
    if not isinstance(value, dict) or any(key not in value for key in CONTEXT_KEYS):
        raise ClarificationError("INVALID_CONTEXT", "Context requires run/item/attempt/node/session/epoch")
    result = {key: value[key] for key in CONTEXT_KEYS}
    for key in ("run_id", "item_id", "node_id", "session_id"):
        identifier(result[key], key)
    nonnegative(result["attempt"], "attempt")
    nonnegative(result["context_epoch"], "context_epoch")
    if "role" in value:
        result["role"] = identifier(value["role"], "role")
    return clone(result)


class ClarificationScheduler:
    def __init__(self, run_id: str, *, max_requests: int = 1, max_reply_calls: int = 2,
                 deadline_seconds: float = 120, clock: Callable[[], float] = time.time):
        self.run_id = identifier(run_id, "run_id")
        self.max_requests = nonnegative(max_requests, "max_requests")
        self.max_reply_calls = nonnegative(max_reply_calls, "max_reply_calls")
        if deadline(0, deadline_seconds) is None:
            raise ClarificationError("INVALID_DEADLINE", "Clarification deadline must be finite")
        self.deadline_seconds = deadline_seconds
        self.clock = clock
        self.requests: dict[str, dict] = {}
        self.frozen = False
        self._lock = threading.RLock()

    def _open(self) -> None:
        if self.frozen:
            raise ClarificationError("FROZEN", "Clarification scheduling is frozen")

    def _find(self, request_id: str, *, check_expiry: bool = True) -> dict:
        if request_id not in self.requests:
            raise ClarificationError("UNKNOWN_REQUEST", "No such clarification request")
        value = self.requests[request_id]
        if check_expiry and value["state"] in PENDING_STATES | {"REPLY_READY"} and self.clock() >= value["expires_at"]:
            value.update(state="FAILED", failure_reason="expired", failed_at=self.clock())
            raise ClarificationError("REQUEST_EXPIRED", "Clarification deadline elapsed")
        return value

    def get(self, request_id: str) -> dict:
        with self._lock:
            return deepcopy(self._find(request_id, check_expiry=False))

    def request(self, source_context: dict, target_role: str, question: str,
                missing_fields: list[str], request_id: str, contract_revision: int) -> dict:
        source = context(source_context)
        identifier(request_id, "request_id")
        identifier(target_role, "target_role")
        identifier(question, "question")
        nonnegative(contract_revision, "contract_revision")
        if source["run_id"] != self.run_id:
            raise ClarificationError("RUN_MISMATCH", "Source belongs to another run")
        if source.get("role") == target_role:
            raise ClarificationError("SELF_REQUEST", "A role cannot wait for its own clarification")
        if not isinstance(missing_fields, list) or any(not isinstance(v, str) or not v for v in missing_fields):
            raise ClarificationError("INVALID_FIELDS", "missing_fields must be a list of nonempty strings")
        payload = {"source_context": source, "target_role": target_role, "question": question,
                   "missing_fields": list(missing_fields), "request_id": request_id,
                   "contract_revision": contract_revision}
        with self._lock:
            self._open()
            old = self.requests.get(request_id)
            if old:
                if old["request_hash"] != digest(payload):
                    raise ClarificationError("REQUEST_CONFLICT", "Request ID already identifies a different question/context")
                return deepcopy(old)
            if len(self.requests) >= self.max_requests:
                raise ClarificationError("REQUEST_BUDGET_EXHAUSTED", "Run clarification request limit reached")
            if any(v["source_context"] == source and v["state"] in PENDING_STATES | {"REPLY_READY"} for v in self.requests.values()):
                raise ClarificationError("SOURCE_ALREADY_WAITING", "Source already has an unresolved request")
            created = self.clock()
            value = {**payload, "run_id": self.run_id, "request_hash": digest(payload),
                     "state": "E_WAITING_REPLY", "created_at": created,
                     "expires_at": deadline(created, self.deadline_seconds),
                     "target_context": None, "reply_tickets": [], "reply_call_count": 0,
                     "reply": None, "resume_count": 0}
            self.requests[request_id] = value
            return deepcopy(value)

    def admit(self, request_id: str, target_context: dict) -> dict:
        target = context(target_context)
        with self._lock:
            self._open()
            value = self._find(request_id)
            if target["run_id"] != self.run_id or target.get("role") != value["target_role"]:
                raise ClarificationError("WRONG_TARGET", "Admitted context must name the requested role and run")
            if value["target_context"] is not None:
                if value["target_context"] != target:
                    raise ClarificationError("ADMISSION_CONFLICT", "Clarifier was already bound to another context")
                if value["state"] not in PENDING_STATES:
                    raise ClarificationError("REQUEST_TERMINAL", "Request no longer permits admission")
                return deepcopy(value)
            if value["state"] != "E_WAITING_REPLY":
                raise ClarificationError("REQUEST_TERMINAL", "Request no longer waits for admission")
            value.update(state="ADMITTED", target_context=target, admitted_at=self.clock())
            return deepcopy(value)

    def mark_reply_started(self, request_id: str, ticket_id: str) -> dict:
        identifier(ticket_id, "ticket_id")
        with self._lock:
            self._open()
            value = self._find(request_id)
            if value["state"] not in {"ADMITTED", "P_REPLYING", "REPLY_READY"}:
                raise ClarificationError("NOT_ADMITTED", "Admit a live clarifier before starting its call")
            if ticket_id in value["reply_tickets"]:
                return deepcopy(value)
            if any(ticket_id in item["reply_tickets"] for item in self.requests.values()):
                raise ClarificationError("TICKET_REUSED", "Reply ticket belongs to another request")
            if len(value["reply_tickets"]) >= self.max_reply_calls:
                value.update(state="FAILED", failure_reason="reply_call_budget_exhausted", failed_at=self.clock())
                raise ClarificationError("REPLY_BUDGET_EXHAUSTED", "Clarifier call limit reached")
            value["reply_tickets"].append(ticket_id)
            # A validated reply may precede a separate finish_phase call. That
            # call still consumes this request's budget and deadline, but must
            # not demote the immutable reply to a replaceable in-flight state.
            value.update(state="REPLY_READY" if value["reply"] is not None else "P_REPLYING",
                         reply_call_count=len(value["reply_tickets"]))
            return deepcopy(value)

    def reply(self, reply_to: str, responder_context: dict, current_source_context: dict,
              answer: str, patch: dict | None = None, *, contract_revision: int) -> dict:
        responder, source = context(responder_context), context(current_source_context)
        identifier(answer, "answer")
        nonnegative(contract_revision, "contract_revision")
        if patch is not None and not isinstance(patch, dict):
            raise ClarificationError("INVALID_PATCH", "Clarification patch must be an object or null")
        payload = {"reply_to": reply_to, "responder_context": responder, "source_context": source,
                   "answer": answer, "patch": clone(patch), "contract_revision": contract_revision}
        with self._lock:
            self._open()
            value = self._find(reply_to)
            if source != value["source_context"]:
                raise ClarificationError("STALE_SOURCE", "Requesting execution context has changed")
            if responder != value["target_context"]:
                raise ClarificationError("WRONG_RESPONDER", "Reply does not match the admitted context")
            if contract_revision != value["contract_revision"]:
                raise ClarificationError("CONTRACT_MISMATCH", "Reply targets a different contract revision")
            if value["reply"] is not None:
                if value["reply_hash"] != digest(payload):
                    raise ClarificationError("REPLY_CONFLICT", "Request already has a different reply")
                return deepcopy(value)
            if value["state"] != "P_REPLYING":
                raise ClarificationError("NOT_REPLYING", "Reply requires a started, admitted clarifier call")
            value.update(state="REPLY_READY", reply=payload, reply_hash=digest(payload), replied_at=self.clock())
            return deepcopy(value)

    def resume(self, request_id: str, current_source_context: dict) -> dict:
        source = context(current_source_context)
        with self._lock:
            self._open()
            value = self._find(request_id)
            if source != value["source_context"]:
                raise ClarificationError("STALE_SOURCE", "Cannot resume a different execution context")
            if value["resume_count"]:
                raise ClarificationError("ALREADY_RESUMED", "Reply has already authorized one continuation")
            if value["state"] != "REPLY_READY":
                raise ClarificationError("REPLY_NOT_READY", "No validated reply is ready")
            value.update(state="RESUMED", resume_count=1, resumed_at=self.clock())
            return deepcopy(value)

    def fail(self, request_id: str, reason: str) -> dict:
        identifier(reason, "reason")
        with self._lock:
            self._open()
            value = self._find(request_id, check_expiry=False)
            if value["state"] == "FAILED":
                if value["failure_reason"] != reason:
                    raise ClarificationError("FAILURE_CONFLICT", "Request already has another failure reason")
                return deepcopy(value)
            if value["state"] == "RESUMED":
                raise ClarificationError("REQUEST_TERMINAL", "Resumed request cannot fail retroactively")
            value.update(state="FAILED", failure_reason=reason, failed_at=self.clock())
            return deepcopy(value)

    def expire(self) -> list[str]:
        with self._lock:
            self._open()
            expired = []
            for key, value in self.requests.items():
                if value["state"] in PENDING_STATES | {"REPLY_READY"} and self.clock() >= value["expires_at"]:
                    value.update(state="FAILED", failure_reason="expired", failed_at=self.clock())
                    expired.append(key)
            return expired

    def freeze(self) -> None:
        with self._lock:
            self.frozen = True

    def snapshot(self) -> dict:
        with self._lock:
            value = {"version": 1, "run_id": self.run_id, "max_requests": self.max_requests,
                     "max_reply_calls": self.max_reply_calls, "deadline_seconds": self.deadline_seconds,
                     "frozen": self.frozen, "requests": clone(self.requests)}
            return {**value, "snapshot_hash": digest(value)}

    @classmethod
    def from_snapshot(cls, snapshot: dict, *, clock: Callable[[], float] = time.time) -> "ClarificationScheduler":
        value = clone(snapshot)
        saved_hash = value.pop("snapshot_hash", None)
        if saved_hash != digest(value) or value.get("version") != 1:
            raise ClarificationError("SNAPSHOT_CORRUPT", "Scheduler snapshot hash or version is invalid")
        obj = cls(value["run_id"], max_requests=value["max_requests"], max_reply_calls=value["max_reply_calls"],
                  deadline_seconds=value["deadline_seconds"], clock=clock)
        if len(value["requests"]) > obj.max_requests:
            raise ClarificationError("SNAPSHOT_CORRUPT", "Snapshot exceeds request limits")
        tickets = set()
        for key, request in value["requests"].items():
            payload = {field: request[field] for field in ("source_context", "target_role", "question", "missing_fields", "request_id", "contract_revision")}
            if key != request["request_id"] or request["run_id"] != obj.run_id or digest(payload) != request["request_hash"]:
                raise ClarificationError("SNAPSHOT_CORRUPT", "Request identity or payload hash mismatch")
            context(request["source_context"])
            if request["target_context"] is not None:
                context(request["target_context"])
            if request["reply"] is not None and digest(request["reply"]) != request["reply_hash"]:
                raise ClarificationError("SNAPSHOT_CORRUPT", "Reply hash mismatch")
            if request["state"] not in PENDING_STATES | {"REPLY_READY", "RESUMED", "FAILED"}:
                raise ClarificationError("SNAPSHOT_CORRUPT", "Unknown request state")
            reply_tickets = request["reply_tickets"]
            if len(reply_tickets) != len(set(reply_tickets)) or tickets.intersection(reply_tickets) or len(reply_tickets) > obj.max_reply_calls:
                raise ClarificationError("SNAPSHOT_CORRUPT", "Reply ticket budget or uniqueness mismatch")
            if request["reply_call_count"] != len(reply_tickets) or request["resume_count"] not in (0, 1):
                raise ClarificationError("SNAPSHOT_CORRUPT", "Stored counters are inconsistent")
            if (request["state"] == "RESUMED") != (request["resume_count"] == 1):
                raise ClarificationError("SNAPSHOT_CORRUPT", "Resume state disagrees with its counter")
            tickets.update(reply_tickets)
        obj.requests, obj.frozen = value["requests"], bool(value["frozen"])
        return obj
