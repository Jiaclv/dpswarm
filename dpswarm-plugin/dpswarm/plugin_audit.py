"""Independent, fail-closed plugin journal; never a work-item event store.

One transaction is one JSONL record. The separate durable head detects loss of a
whole valid suffix as well as torn writes. A crash between journal and head sync
is intentionally an unavailable ledger, not permission to discard a reservation.
The caller owns semantic budget decisions and submits them using compare-and-swap.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path, PurePosixPath, PureWindowsPath


VERSION = 1
SCHEMA = "dpswarm-plugin-audit-v1"
ZERO_HASH = "0" * 64
MAX_SAFE_INTEGER = 2 ** 53 - 1
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z")
EVENT_TYPES = frozenset({
    *("dpswarm/worker-budget-" + suffix for suffix in (
        "allocation", "allocation-bound", "team-run", "team-run-ended",
        "team-run-resumed", "team-run-resume-ended",
        "frozen", "failure", "admitted", "settled", "closeout", "denied", "rework-revoked")),
    "dpswarm/worker-diagnostic",
    "dpswarm/fixed-team-binding", "dpswarm/worker-rework",
    "dpswarm/route-bound",
    "dpswarm/handoff-profile", "dpswarm/write-scope",
    "dpswarm/artifact-ready-manifest", "dpswarm/artifact-consumed-manifest",
    *("dpswarm/mailbox-" + suffix for suffix in ("queued", "delivered", "rejected")),
    *("dpswarm/verification-" + suffix for suffix in (
        "required", "binding", "superseded", "takeover", "recovery")),
    "dpswarm/team-required-admission",
    *("dpswarm/team-required-" + suffix for suffix in ("bound", "started", "finished", "amended", "continued")),
    "dpswarm/cm-team", "dpswarm/cm-start", "dpswarm/cm-end", "dpswarm/cm-usage",
})


class PluginAuditError(RuntimeError):
    def __init__(self, code, message, status=409, **details):
        super().__init__(message)
        self.code, self.status, self.details = code, status, details

    def response(self):
        return {"ok": False, "error": self.code, "message": str(self), **self.details}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _hash(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("Non-finite JSON number")

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def valid_session_id(value):
    return isinstance(value, str) and IDENTIFIER.fullmatch(value) is not None



def _validate_native_audit_event(kind, data, root_session_id):
    """Validate actual native emitters; these records confer no acceptance authority."""
    def require(condition, detail):
        if not condition:
            raise PluginAuditError("PLUGIN_AUDIT_INVALID_NATIVE_EVENT", f"{kind}: {detail}", 400)

    def string(value, limit=4096):
        return (isinstance(value, str) and bool(value.strip()) and len(value) <= limit
                and not any(ord(c) < 32 or ord(c) == 127 for c in value))

    def integer(value, minimum=0, maximum=MAX_SAFE_INTEGER):
        return type(value) is int and minimum <= value <= maximum

    def strings(value, limit=64, width=4096, nonempty=True):
        return (isinstance(value, list) and (not nonempty or bool(value)) and len(value) <= limit
                and all(string(item, width) for item in value))

    def fields(required, optional=()):
        require(set(required).issubset(data) and not set(data) - set(required) - set(optional),
                "missing or unsupported fields")

    def sha256(value):
        return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None

    def relative_path(value):
        if not string(value, 512) or "\\" in value or ":" in value or value.startswith("/"):
            return False
        return all(part not in ("", ".", "..") and not part.endswith((" ", "."))
                   and not any(c in part for c in '<>"|?*')
                   and not re.match(r"(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", part)
                   for part in value.split("/"))

    if kind == "dpswarm/handoff-profile":
        fields({"root_session_id", "artifact_id", "phase", "profile", "decider", "upstreams"}, {"via"})
        require(data["root_session_id"] == root_session_id and string(data["artifact_id"]), "invalid artifact scope")
        require(data["phase"] is None or string(data["phase"]), "phase must be a name or null")
        require(data["profile"] in ("verbatim", "semantic") and data["decider"] == "rule", "invalid handoff classification")
        require(strings(data["upstreams"]) and len(set(data["upstreams"])) == len(data["upstreams"]), "invalid upstream ids")
        require("via" not in data or data["via"] == "wake", "unsupported handoff carrier")
    elif kind == "dpswarm/write-scope":
        fields({"root_session_id", "worker_session_id", "subtask", "scopes", "run_id", "claimed_at", "state", "version"})
        require(data["root_session_id"] == root_session_id and valid_session_id(data["worker_session_id"]), "invalid worker scope")
        require(string(data["subtask"]) and strings(data["scopes"], width=1024), "invalid subtask or write scopes")
        require(data["run_id"] is None or string(data["run_id"], 128), "invalid run id")
        require(integer(data["claimed_at"]) and data["state"] == "claimed" and type(data["version"]) is int and data["version"] == 1,
                "invalid write claim version or time")
    elif kind in ("dpswarm/artifact-ready-manifest", "dpswarm/artifact-consumed-manifest"):
        common = {"artifact_id", "manifest_digest"}
        if kind.endswith("ready-manifest"):
            fields(common | {"candidate_files", "view_path"}, {"root_session_id"})
            view = data["view_path"]
            require(string(view, 8192) and (PurePosixPath(view).is_absolute() or PureWindowsPath(view).is_absolute()),
                    "view_path must be an absolute local path")
            files = data["candidate_files"]
            require(isinstance(files, list) and 0 < len(files) <= 64, "candidate_files must contain 1 to 64 entries")
            seen, size = set(), 0
            for file in files:
                require(isinstance(file, dict) and {"path", "operation", "sha256", "size"}.issubset(file)
                        and not set(file) - {"path", "operation", "sha256", "size", "blob_ref"}, "invalid manifest file fields")
                require(relative_path(file["path"]), "manifest file must have a safe relative path")
                key = file["path"].casefold()
                require(key not in seen, "duplicate case-insensitive manifest path")
                seen.add(key)
                require(file["operation"] in ("file", "deleted") and integer(file["size"], maximum=3 * 1024 * 1024),
                        "invalid file operation or size")
                if file["operation"] == "file":
                    require(sha256(file["sha256"]), "file hash must be SHA256")
                    require("blob_ref" not in file or file["blob_ref"] == file["sha256"], "blob reference must match file hash")
                else:
                    require(file["sha256"] is None and file["size"] == 0 and "blob_ref" not in file,
                            "deleted paths must not reference content")
                size += file["size"]
            require(size <= 3 * 1024 * 1024, "manifest exceeds the total snapshot byte bound")
            require(not any(path.startswith(other + "/") for path in seen for other in seen if path != other),
                    "manifest paths overlap as file and directory")
        else:
            fields(common | {"worker_session_id", "paths"}, {"root_session_id"})
            require(valid_session_id(data["worker_session_id"]), "invalid consuming worker")
            paths = data["paths"]
            require(strings(paths) and all(relative_path(path) for path in paths), "consumed paths must be safe relative paths")
            require(len({path.casefold() for path in paths}) == len(paths), "duplicate consumed paths")
        require(string(data["artifact_id"]) and sha256(data["manifest_digest"]), "invalid artifact id or manifest digest")
    elif kind.startswith("dpswarm/mailbox-"):
        common = {"version", "root_session_id", "run_id", "message_id", "from", "to", "kind", "at"}
        require(type(data.get("version")) is int and data["version"] == 1
                and data.get("root_session_id") == root_session_id and integer(data.get("at")), "invalid mailbox scope or time")
        if kind == "dpswarm/mailbox-rejected":
            fields(common | {"code", "detail"})
            # Rejected audit records intentionally preserve invalid or absent
            # caller fields. They must never be mistaken for a queued message.
            require(all(data[key] is None or isinstance(data[key], str) for key in ("run_id", "message_id", "from", "to", "kind")),
                    "rejected message fields must be strings or null")
            require(string(data["code"], 160) and isinstance(data["detail"], str) and len(data["detail"]) <= 65536,
                    "invalid rejection diagnosis")
            return
        require(string(data.get("run_id"), 128) and isinstance(data.get("message_id"), str)
                and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", data["message_id"]) is not None,
                "invalid mailbox run or message id")
        require(all(isinstance(data.get(key), str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", data[key]) is not None
                    for key in ("from", "to")), "invalid mailbox members")
        require(data.get("kind") in ("fact", "clarify", "block"), "mailbox records cannot carry control commands")
        delivery = "inject" if data["kind"] == "fact" else "followup"
        if kind == "dpswarm/mailbox-queued":
            fields(common | {"delivery", "refs", "ts", "pending_for_target"})
            require(data["delivery"] == delivery and strings(data["refs"], limit=16, width=256, nonempty=False),
                    "invalid queued delivery or references")
            require(integer(data["ts"]) and integer(data["pending_for_target"], minimum=1), "invalid queue count or time")
        else:
            fields(common | {"via", "carrier"})
            require(data["via"] in (delivery, "acknowledge") and string(data["carrier"], 128), "invalid delivery carrier")


def validate_transaction(body, root_session_id):
    if not valid_session_id(root_session_id):
        raise PluginAuditError("INVALID_SESSION", "Invalid DSH session identifier", 400)
    if not isinstance(body, dict) or body.get("root_session_id") != root_session_id:
        raise PluginAuditError("SESSION_SCOPE_MISMATCH", "The audit root must match X-DPSwarm-Session", 400)
    if set(body) != {"root_session_id", "expected_revision", "transaction_id", "events"}:
        raise PluginAuditError("PLUGIN_AUDIT_INVALID_TRANSACTION", "Unexpected or missing transaction fields", 400)
    revision = body["expected_revision"]
    if type(revision) is not int or not 0 <= revision <= MAX_SAFE_INTEGER:
        raise PluginAuditError("PLUGIN_AUDIT_INVALID_TRANSACTION", "expected_revision must be a nonnegative safe integer", 400)
    if not valid_session_id(body["transaction_id"]):
        raise PluginAuditError("PLUGIN_AUDIT_INVALID_TRANSACTION", "Invalid transaction_id", 400)
    events = body["events"]
    if not isinstance(events, list) or not events:
        raise PluginAuditError("PLUGIN_AUDIT_INVALID_TRANSACTION", "events must be a nonempty array", 400)
    for event in events:
        if not isinstance(event, dict) or set(event) != {"type", "data"} or not isinstance(event["data"], dict):
            raise PluginAuditError("PLUGIN_AUDIT_INVALID_EVENT", "Every event requires only type and object data", 400)
        if not isinstance(event["type"], str) or event["type"] not in EVENT_TYPES:
            raise PluginAuditError("PLUGIN_AUDIT_INVALID_EVENT", "Event type is not in the plugin audit vocabulary", 400)
        if "root_session_id" in event["data"] and event["data"]["root_session_id"] != root_session_id:
            raise PluginAuditError("SESSION_SCOPE_MISMATCH", "Event root differs from the audit root", 400)
        data = event["data"]
        _validate_native_audit_event(event["type"], data, root_session_id)
        if (event["type"] == "dpswarm/worker-budget-allocation"
                and data.get("budget_origin") == "source_remaining_report_repair"):
            remaining, profile = data.get("source_remaining"), data.get("profile", {})
            if (data.get("version") != 3 or data.get("authority") != "fixed-team-rework"
                    or data.get("decided_by") != "source_worker_remaining_budget"
                    or data.get("label") not in ("tester", "reviewer")
                    or not valid_session_id(data.get("source_worker_session_id"))
                    or not valid_session_id(data.get("source_allocation_id"))):
                raise PluginAuditError("PLUGIN_AUDIT_INVALID_REPORT_REPAIR", "Invalid report repair source or verification role", 400)
            if profile.get("mode") == "unlimited":
                valid_remaining = remaining is None
            else:
                valid_remaining = (isinstance(remaining, dict) and set(remaining) == {"tokens", "calls"}
                    and all(type(remaining[key]) is int and remaining[key] > 0 for key in remaining)
                    and profile.get("tokenLimit") == remaining.get("tokens")
                    and profile.get("callLimit") == remaining.get("calls"))
            if not valid_remaining:
                raise PluginAuditError("PLUGIN_AUDIT_INVALID_REPORT_REPAIR", "Repair profile must exactly match source remaining budget", 400)
        if event["type"] == "dpswarm/route-bound":
            data = event["data"]
            if (set(data) != {"protocol", "root_session_id", "owner_session_id", "child_session_id", "parent_session_id", "label", "route"}
                    or data.get("protocol") != "fixed-role-route-v1"
                    or data.get("root_session_id") != root_session_id
                    or data.get("parent_session_id") != root_session_id
                    or not valid_session_id(data.get("child_session_id"))
                    or data.get("child_session_id") == root_session_id
                    or data.get("owner_session_id") != data.get("child_session_id")
                    or not isinstance(data.get("label"), str) or not data["label"].startswith("dpswarm:")
                    or len(data["label"]) > 4000):
                raise PluginAuditError("PLUGIN_AUDIT_INVALID_ROUTE", "Route binding identity or protocol is invalid", 400)
            route = data["route"]
            if (not isinstance(route, dict) or not {"provider", "model"}.issubset(route)
                    or set(route) - {"provider", "model", "reasoningEffort"}
                    or any(not isinstance(value, str) or not value.strip() or len(value) > 1000 for value in route.values())):
                raise PluginAuditError("PLUGIN_AUDIT_INVALID_ROUTE", "Route binding requires an exact provider/model and optional effort", 400)
    try:
        # Copy caller objects before either an await or a durable append.
        return _strict_json(_canonical(body))
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise PluginAuditError("PLUGIN_AUDIT_INVALID_TRANSACTION", "Transaction is not finite UTF-8 JSON", 400) from error


class PluginAuditStore:
    """Lifetime OS writer lock plus thread-serialized CAS and idempotent commits."""

    def __init__(self, directory: Path, root_session_id: str, *, create=False):
        if not valid_session_id(root_session_id):
            raise PluginAuditError("INVALID_SESSION", "Invalid DSH session identifier", 400)
        self.directory = Path(directory)
        self.path = self.directory / "journal.jsonl"
        self.head_path = self.directory / "head.json"
        self.identity_path = self.directory.with_name(self.directory.name + ".identity")
        self.root_session_id = root_session_id
        self._lock = threading.RLock()
        self._lock_fh = None
        self._closed = False
        self._failed = False
        self._events = []
        self._transactions = {}
        self.revision = 0
        self.head_hash = ZERO_HASH
        if not self.directory.exists():
            if self.identity_path.exists():
                raise PluginAuditError("PLUGIN_AUDIT_MISSING", "The initialized audit directory is missing")
            if not create:
                raise PluginAuditError("PLUGIN_AUDIT_NOT_FOUND", "No audit ledger exists for this root", 404)
            self.directory.parent.mkdir(parents=True, exist_ok=True)
            try:
                with self.identity_path.open("xb") as stream:
                    stream.write(_canonical(self._identity()) + b"\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                self.directory.mkdir(exist_ok=False)
                fresh = True
            except FileExistsError as error:
                raise PluginAuditError("PLUGIN_AUDIT_INITIALIZING", "Another writer has initialized this audit identity") from error
        else:
            fresh = False
        try:
            self._acquire_writer_lock()
            if fresh:
                header = {"schema": SCHEMA, "version": VERSION, "kind": "header",
                          "root_session_id": root_session_id, "revision": 0, "previous_hash": ZERO_HASH}
                header["hash"] = _hash(header)
                self._write_initial(header)
            elif not self.path.is_file() or not self.head_path.is_file():
                raise PluginAuditError("PLUGIN_AUDIT_MISSING", "Existing audit directory is missing its journal or durable head")
            self._load()
        except BaseException:
            self.close()
            raise

    def _acquire_writer_lock(self):
        stream = None
        try:
            stream = (self.directory / "writer.lock").open("a+b")
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if stream is not None:
                stream.close()
            raise PluginAuditError("PLUGIN_AUDIT_LOCKED", "Another process owns the plugin audit writer lock") from error
        self._lock_fh = stream

    def _sync_directory(self):
        # Windows does not expose fsync(directory) through the standard library.
        # Both files are fsynced on every platform; a missing rename fails closed.
        if os.name != "nt":
            fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def _identity(self):
        return {"schema": SCHEMA, "version": VERSION, "root_session_id": self.root_session_id}

    def _head(self, revision, head_hash, raw):
        return {"schema": SCHEMA, "version": VERSION, "root_session_id": self.root_session_id,
                "revision": revision, "head_hash": head_hash, "byte_length": len(raw),
                "file_sha256": hashlib.sha256(raw).hexdigest()}

    def _write_head(self, head):
        temporary = self.directory / "head.pending"
        if temporary.exists():
            raise PluginAuditError("PLUGIN_AUDIT_INCOMPLETE_COMMIT", "A previous head update is unresolved")
        with temporary.open("xb") as stream:
            stream.write(_canonical(head) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.head_path)
        self._sync_directory()

    def _write_initial(self, header):
        raw = _canonical(header) + b"\n"
        with self.path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        self._write_head(self._head(0, header["hash"], raw))

    def _read_disk(self):
        try:
            identity = _strict_json(self.identity_path.read_bytes())
            if identity != self._identity():
                raise ValueError("Audit identity/version mismatch")
            if (self.directory / "head.pending").exists():
                raise PluginAuditError("PLUGIN_AUDIT_INCOMPLETE_COMMIT", "A previous head update is unresolved")
            raw = self.path.read_bytes()
            head_raw = self.head_path.read_bytes()
            if not raw or not raw.endswith(b"\n") or not head_raw.endswith(b"\n"):
                raise ValueError("Empty or truncated audit file")
            head = _strict_json(head_raw)
            if not isinstance(head, dict) or head.get("file_sha256") != hashlib.sha256(raw).hexdigest() or head.get("byte_length") != len(raw):
                raise ValueError("Journal and durable head disagree")
            return raw, head
        except FileNotFoundError as error:
            raise PluginAuditError("PLUGIN_AUDIT_MISSING", "Existing audit journal or durable head is missing") from error
        except (ValueError, TypeError, UnicodeError, RecursionError) as error:
            raise PluginAuditError("PLUGIN_AUDIT_CORRUPT", "Audit ledger failed integrity validation") from error

    def _load(self):
        raw, head = self._read_disk()
        try:
            records = [_strict_json(line) for line in raw.splitlines()]
            first = records[0]
            expected_header = {"schema": SCHEMA, "version": VERSION, "kind": "header",
                               "root_session_id": self.root_session_id, "revision": 0, "previous_hash": ZERO_HASH}
            expected_header["hash"] = _hash(expected_header)
            if first != expected_header:
                raise ValueError("Audit header identity/version/hash mismatch")
            self.head_hash = first["hash"]
            for record in records[1:]:
                if set(record) != {"schema", "version", "kind", "root_session_id", "revision", "previous_hash", "transaction_id", "expected_revision", "events", "hash"}:
                    raise ValueError("Unknown transaction envelope")
                payload = {k: record[k] for k in ("root_session_id", "expected_revision", "transaction_id", "events")}
                validate_transaction(payload, self.root_session_id)
                unsigned = {k: v for k, v in record.items() if k != "hash"}
                if (record["schema"] != SCHEMA or record["version"] != VERSION or record["kind"] != "transaction"
                        or record["revision"] != self.revision + 1 or record["expected_revision"] != self.revision
                        or record["previous_hash"] != self.head_hash or record["hash"] != _hash(unsigned)
                        or record["transaction_id"] in self._transactions):
                    raise ValueError("Invalid transaction chain")
                self._apply(record, payload)
            expected_head = self._head(self.revision, self.head_hash, raw)
            if head != expected_head:
                raise ValueError("Durable head identity/revision mismatch")
            self._disk_head = expected_head
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, PluginAuditError) as error:
            raise PluginAuditError("PLUGIN_AUDIT_CORRUPT", "Audit ledger failed chain validation") from error

    def _apply(self, record, payload):
        self.revision, self.head_hash = record["revision"], record["hash"]
        self._transactions[record["transaction_id"]] = (_hash(payload), self.revision)
        for event in record["events"]:
            self._events.append({"seq": len(self._events) + 1, "type": event["type"], "data": event["data"],
                                 "revision": self.revision, "transaction_id": record["transaction_id"]})

    def _verify_live(self):
        if self._closed or self._failed:
            raise PluginAuditError("PLUGIN_AUDIT_UNAVAILABLE", "Audit writer is closed or has an unresolved write failure", 503)
        raw, head = self._read_disk()
        if head != self._disk_head:
            raise PluginAuditError("PLUGIN_AUDIT_CORRUPT", "Audit data changed outside the locked writer")
        return raw

    def _snapshot(self):
        # Projected objects must not permit callers to mutate authoritative state.
        return {"root_session_id": self.root_session_id, "version": VERSION,
                "revision": self.revision, "events": _strict_json(_canonical(self._events)), "head_hash": self.head_hash}

    def read(self):
        with self._lock:
            self._verify_live()
            return self._snapshot()

    def append(self, body):
        payload = validate_transaction(body, self.root_session_id)
        with self._lock:
            raw = self._verify_live()
            prior = self._transactions.get(payload["transaction_id"])
            if prior is not None:
                if prior[0] != _hash(payload):
                    raise PluginAuditError("PLUGIN_AUDIT_TRANSACTION_MISMATCH", "transaction_id was already used for a different payload",
                                           revision=self.revision, head_hash=self.head_hash)
                return {**self._snapshot(), "transaction_revision": prior[1], "idempotent": True}
            if payload["expected_revision"] != self.revision:
                raise PluginAuditError("PLUGIN_AUDIT_REVISION_CONFLICT", "Expected revision differs from the current audit revision",
                                       revision=self.revision, head_hash=self.head_hash)
            if self.revision >= MAX_SAFE_INTEGER:
                raise PluginAuditError("PLUGIN_AUDIT_UNAVAILABLE", "Audit revision is exhausted", 503)
            record = {"schema": SCHEMA, "version": VERSION, "kind": "transaction", **payload,
                      "revision": self.revision + 1, "previous_hash": self.head_hash}
            record["hash"] = _hash(record)
            line = _canonical(record) + b"\n"
            head = self._head(record["revision"], record["hash"], raw + line)
            try:
                # r+b cannot recreate a deleted ledger; verify before appending.
                with self.path.open("r+b") as stream:
                    stream.seek(0, os.SEEK_END)
                    if stream.tell() != len(raw):
                        raise PluginAuditError("PLUGIN_AUDIT_CORRUPT", "Audit size changed before append")
                    stream.write(line)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._write_head(head)
            except BaseException:
                self._failed = True
                raise
            self._apply(record, payload)
            self._disk_head = head
            return {**self._snapshot(), "transaction_revision": self.revision, "idempotent": False}

    def close(self):
        with self._lock:
            self._closed = True
            if self._lock_fh is not None:
                try:
                    if os.name == "nt":
                        import msvcrt
                        self._lock_fh.seek(0)
                        msvcrt.locking(self._lock_fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_UN)
                finally:
                    self._lock_fh.close()
                    self._lock_fh = None
