"""DSH bridge sidecar with separate control-plane state per host session.

The session header selects state; the shared local bearer token authorizes it.
This isolates control state, not the host's project files or model history.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .server import Handler, PanelState, MAX_BODY_BYTES
from .control import ControlPlaneError
from .plugin_audit import PluginAuditError, PluginAuditStore, _strict_json, validate_transaction

BRIDGE = {"protocol": "dpswarm-dsh-fixed-v1", "session_isolation": True, "plugin_audit_v1": True, "host_catalog_v1": True,
          "context_management": "DSH plugin owns optional CM; query dpswarm_status for session enablement and adoption"}


class SessionState(PanelState):
    def __init__(self, workspace: Path, session_id: str, token: str):
        self.host_session_id = session_id
        super().__init__(workspace)
        self.token = token

    def bind_execution_root(self, body):
        if body.get("parent_session_id") != self.host_session_id:
            return False, {"ok": False, "error": "SESSION_SCOPE_MISMATCH",
                           "message": "The execution parent must match the selected host session"}
        return super().bind_execution_root(body)


class SessionHub:
    def __init__(self, workspace: Path):
        self.root = PanelState(workspace)
        self._lock = threading.RLock()
        self._states = {}
        self._audits = {}

    def audit(self, session_id: str, *, create: bool = False):
        # Audit-only roots must not instantiate PanelState/ControlPlane: doing so
        # would create a business task merely to account for a native worker.
        if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", session_id):
            raise PluginAuditError("INVALID_SESSION", "Invalid DSH session identifier", 400)
        with self._lock:
            if session_id not in self._audits:
                key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
                directory = self.root.workspace / "sessions" / key / "plugin-audit"
                self._audits[session_id] = PluginAuditStore(directory, session_id, create=create)
            return self._audits[session_id]

    def get(self, session_id: str, *, create: bool = False):
        if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", session_id):
            raise ValueError("Invalid DSH session identifier")
        with self._lock:
            if session_id in self._states:
                return self._states[session_id]
            # Never use a session identifier as a filesystem path.
            key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
            workspace = self.root.workspace / "sessions" / key
            if not create and not (workspace / "events.jsonl").is_file():
                return None
            state = SessionState(workspace, session_id, self.root.token)
            self._states[session_id] = state
            return state

    def close(self):
        with self._lock:
            for audit in self._audits.values():
                audit.close()
            self._audits.clear()
            for state in self._states.values():
                state.cp.close()
            self._states.clear()
            self.root.cp.close()


class SessionHandler(Handler):
    def _host_catalog(self):
        self.state = self.server.hub.root
        if not self._origin_allowed():
            self._forbid()
            return
        if not self._authorized():
            self._unauthorized()
            return
        session_id = self.headers.get("X-DPSwarm-Session")
        if not session_id:
            self._json({"ok": False, "error": "SESSION_REQUIRED"}, 400)
            return
        try:
            if self.headers.get("Transfer-Encoding"):
                raise ControlPlaneError("HOST_CATALOG_INVALID", "Chunked catalog bodies are unsupported")
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError as exc:
                raise ControlPlaneError("HOST_CATALOG_INVALID", "Invalid Content-Length") from exc
            if length > MAX_BODY_BYTES:
                self._json({"ok": False, "error": "BODY_TOO_LARGE"}, 413)
                return
            if length < 1:
                raise ControlPlaneError("HOST_CATALOG_INVALID", "A host catalog body is required")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ControlPlaneError("HOST_CATALOG_INVALID", "Incomplete host catalog body")
            try:
                body = _strict_json(raw)
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise ControlPlaneError("HOST_CATALOG_INVALID", "Invalid UTF-8 JSON host catalog") from exc
            # Validate before creating control state for an invalid request.
            PanelState.validate_host_catalog(body)
            try:
                state = self.server.hub.get(session_id, create=True)
            except ValueError as exc:
                self._json({"ok": False, "error": "INVALID_SESSION", "message": str(exc)}, 400)
                return
            self.state = state
            self._json(state.sync_host_catalog(body))
        except ControlPlaneError as exc:
            status = 503 if exc.code == "HOST_CATALOG_STORAGE_UNAVAILABLE" else 400
            self._json({"ok": False, "error": exc.code, "message": str(exc)}, status)
        except OSError:
            self._json({"ok": False, "error": "HOST_CATALOG_STORAGE_UNAVAILABLE",
                        "message": "Host catalog storage is unavailable"}, 503)

    def _plugin_audit(self, write: bool):
        self.state = self.server.hub.root
        if not self._origin_allowed():
            self._forbid()
            return
        if not self._authorized():
            self._unauthorized()
            return
        session_id = self.headers.get("X-DPSwarm-Session")
        if not session_id:
            self._json({"ok": False, "error": "SESSION_REQUIRED"}, 400)
            return
        try:
            if write:
                if self.headers.get("Transfer-Encoding"):
                    raise PluginAuditError("PLUGIN_AUDIT_INVALID_TRANSACTION", "Chunked transaction bodies are unsupported", 400)
                try:
                    length = int(self.headers.get("Content-Length") or "0")
                except ValueError as error:
                    raise PluginAuditError("PLUGIN_AUDIT_INVALID_TRANSACTION", "Invalid Content-Length", 400) from error
                if length > MAX_BODY_BYTES:
                    raise PluginAuditError("BODY_TOO_LARGE", "Audit transaction body is too large", 413)
                if length < 1:
                    raise PluginAuditError("PLUGIN_AUDIT_INVALID_TRANSACTION", "A transaction body is required", 400)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise PluginAuditError("PLUGIN_AUDIT_INVALID_TRANSACTION", "Incomplete transaction body", 400)
                try:
                    body = _strict_json(raw)
                except (ValueError, UnicodeError, RecursionError) as error:
                    raise PluginAuditError("PLUGIN_AUDIT_INVALID_TRANSACTION", "Invalid UTF-8 JSON transaction", 400) from error
                body = validate_transaction(body, session_id)
                audit = self.server.hub.audit(session_id, create=body["expected_revision"] == 0)
                self._json(audit.append(body))
            else:
                self._json(self.server.hub.audit(session_id).read())
        except PluginAuditError as error:
            self._json(error.response(), error.status)
        except OSError:
            self._json({"ok": False, "error": "PLUGIN_AUDIT_IO_ERROR",
                        "message": "Audit storage is unavailable; the transaction is not acknowledged"}, 503)

    def _prepare(self, write: bool):
        self.state = self.server.hub.root
        if not self._origin_allowed():
            self._forbid()
            return False
        if write and not self._authorized():
            self._unauthorized()
            return False
        path = urlsplit(self.path).path
        session_id = self.headers.get("X-DPSwarm-Session")
        if not session_id:
            # The unscoped connection is for discovery and the human panel.
            if write:
                self._json({"ok": False, "error": "SESSION_REQUIRED"}, 400)
                return False
            if path in ("/", "/index.html"):
                self.path = path
            return True
        if not write and path != "/api/status" and not self._authorized():
            self._unauthorized()
            return False
        try:
            state = self.server.hub.get(session_id, create=write and path == "/api/execution/root")
        except ValueError as error:
            self._json({"ok": False, "error": "INVALID_SESSION", "message": str(error)}, 400)
            return False
        except ControlPlaneError as error:
            self._json({"ok": False, "error": error.code, "message": str(error)}, 503)
            return False
        if state is None:
            if not write and path == "/api/status":
                self._json({"session_id": session_id, "state": "not_started", "snapshot": None,
                            "bridge": BRIDGE})
            else:
                self._json({"ok": False, "error": "SESSION_NOT_STARTED"}, 404)
            return False
        self.state = state
        return True

    def _json(self, obj, code=200):
        if urlsplit(self.path).path == "/api/status" and isinstance(obj, dict) and code == 200:
            obj = {**obj, "bridge": BRIDGE, "session_id": self.headers.get("X-DPSwarm-Session")}
        super()._json(obj, code)

    def do_GET(self):
        if urlsplit(self.path).path == "/api/plugin-audit":
            self._plugin_audit(False)
            return
        if self._prepare(False):
            super().do_GET()

    def do_POST(self):
        if urlsplit(self.path).path == "/api/models/host-catalog":
            self._host_catalog()
            return
        if urlsplit(self.path).path == "/api/plugin-audit":
            self._plugin_audit(True)
            return
        if self._prepare(True):
            super().do_POST()

    def do_OPTIONS(self):
        self.state = self.server.hub.root
        if not self._origin_allowed():
            self._forbid()
            return
        self.send_response(204)
        self._apply_cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-DPSwarm-Session")
        self.end_headers()


class SessionHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False
    block_on_close = True
    daemon_threads = False


def create_server(workspace: Path, port: int = 8791):
    hub = SessionHub(workspace)
    try:
        server = SessionHTTPServer(("127.0.0.1", port), SessionHandler)
    except BaseException:
        hub.close()
        raise
    server.hub = hub
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    server = create_server(args.workspace, args.port)
    print(f"DPSwarm fixed-team sidecar: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        server.hub.close()


if __name__ == "__main__":
    main()
