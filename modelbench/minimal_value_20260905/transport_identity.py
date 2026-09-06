"""Secret-free identity of the external executors used by the frozen transports.

This is a checkpoint, not a lock against replacing an executable mid-call.
No provider request or Codex model command is issued. Only Node runs a fixed
metadata resolver. Credential values and authentication files are not inspected.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit

from modelbench.team_eval20260903 import transports as original

PROTOCOL = "minimum_value_transport_identity_v1"


class TransportIdentityError(RuntimeError):
    """Messages identify fields only, never rejected URLs or subprocess output."""


def normalize_endpoint(value, *, field="endpoint"):
    """Canonicalize HTTP origins and trailing slashes, preserving path case.

    The real transport appends /chat/completions. Queries and fragments are
    unsupported entirely; rejecting them also prevents credentials in a URL
    from entering an otherwise public identity artifact.
    """
    if not isinstance(value, str) or not value.strip():
        raise TransportIdentityError(f"{field}: endpoint is missing")
    value = value.strip()
    try:
        if any(ord(char) < 33 or ord(char) == 127 for char in value) or "\\" in value:
            raise ValueError()
        parsed = urlsplit(value)
        if (parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or "?" in value or "#" in value):
            raise ValueError()
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        if not re.fullmatch(r"[a-z0-9._:-]+", host):
            raise ValueError()
        port = parsed.port
        scheme = parsed.scheme.lower()
        if ":" in host:
            host = "[" + host + "]"
        if port is not None and port != (443 if scheme == "https" else 80):
            host += ":" + str(port)
        return urlunsplit((scheme, host, parsed.path.rstrip("/"), "", ""))
    except (ValueError, UnicodeError):
        raise TransportIdentityError(f"{field}: endpoint URL is not safe to record") from None


def _endpoints():
    # Match the transport's actual env > keyconfig lookup and DeepSeek default.
    # Never request *_API_KEY, and never serialize keyconfig or its raw file.
    try:
        glm = original._keyconfig("GLM_BASE_URL")
        deepseek = original._keyconfig("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
    except Exception:
        raise TransportIdentityError("endpoints: configuration lookup failed") from None
    glm = normalize_endpoint(glm, field="endpoints.glm")
    if "/coding/" not in glm.rstrip("/") + "/":
        raise TransportIdentityError("endpoints.glm: configured coding endpoint is required")
    deepseek = normalize_endpoint(deepseek, field="endpoints.deepseek")
    result = {"glm": {"base_url": glm, "request_url": glm + "/chat/completions"},
              "deepseek": {"base_url": deepseek, "request_url": deepseek + "/chat/completions"}}
    # Codex ignores user config, but an explicit standard API URL override is
    # still external configuration. This records the override, not a claim
    # about the server-side route selected by a ChatGPT subscription.
    override = os.environ.get("OPENAI_BASE_URL", "").strip()
    result["codex_openai_base_url_override"] = normalize_endpoint(override, field="endpoints.codex_openai_base_url_override") if override else None
    return result


def _file_identity(path, field):
    try:
        resolved = Path(path).resolve(strict=True)
        if not resolved.is_file():
            raise OSError()
        digest = hashlib.sha256()
        with resolved.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return {"path": str(resolved), "sha256": digest.hexdigest()}
    except (OSError, ValueError, TypeError):
        raise TransportIdentityError(f"{field}: executable or metadata file is unavailable") from None


def _package_identity(path, field):
    identity = _file_identity(path, field)
    try:
        value = json.loads(Path(identity["path"]).read_text(encoding="utf-8"))
        name, version = value["name"], value["version"]
        if (not isinstance(name, str) or not re.fullmatch(r"@openai/codex(?:-[a-z0-9-]+)?", name)
                or not isinstance(version, str)
                or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", version)):
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError):
        raise TransportIdentityError(f"{field}: package identity is invalid") from None
    if _file_identity(path, field) != identity:
        raise TransportIdentityError(f"{field}: file changed during identity capture")
    return {**identity, "name": name, "version": version}


# Mirrors the installed codex.js resolver: createRequire anchored at the real
# launcher, optional platform package first, then the launcher's vendor folder.
# This does NOT import or execute codex.js and cannot spawn the native Codex CLI.
_NODE_RESOLVER = r'''
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const { pathToFileURL } = require('node:url');
const launcher = fs.realpathSync(process.argv[1]);
const localRequire = createRequire(pathToFileURL(launcher));
const platform = process.platform, arch = process.arch;
const triples = {
  'linux:x64':'x86_64-unknown-linux-musl', 'android:x64':'x86_64-unknown-linux-musl',
  'linux:arm64':'aarch64-unknown-linux-musl', 'android:arm64':'aarch64-unknown-linux-musl',
  'darwin:x64':'x86_64-apple-darwin', 'darwin:arm64':'aarch64-apple-darwin',
  'win32:x64':'x86_64-pc-windows-msvc', 'win32:arm64':'aarch64-pc-windows-msvc',
};
const packages = {
  'x86_64-unknown-linux-musl':'@openai/codex-linux-x64',
  'aarch64-unknown-linux-musl':'@openai/codex-linux-arm64',
  'x86_64-apple-darwin':'@openai/codex-darwin-x64',
  'aarch64-apple-darwin':'@openai/codex-darwin-arm64',
  'x86_64-pc-windows-msvc':'@openai/codex-win32-x64',
  'aarch64-pc-windows-msvc':'@openai/codex-win32-arm64',
};
const triple = triples[platform + ':' + arch];
if (!triple) process.exit(2);
const specifier = packages[triple];
let packageJson = null, vendor;
try {
  packageJson = localRequire.resolve(specifier + '/package.json');
  vendor = path.join(path.dirname(packageJson), 'vendor');
} catch {
  vendor = path.join(path.dirname(launcher), '..', 'vendor');
}
const native = path.join(vendor, triple, 'bin', platform === 'win32' ? 'codex.exe' : 'codex');
if (!fs.existsSync(native)) process.exit(3);
process.stdout.write(JSON.stringify({node_path:process.execPath,node_version:process.version,
  platform,arch,target_triple:triple,native_path:fs.realpathSync(native),
  native_package_json:packageJson ? fs.realpathSync(packageJson) : null,
  native_package_specifier:specifier}));
'''


def _node_identity_probe(launcher):
    if os.environ.get("NODE_OPTIONS", "").strip():
        raise TransportIdentityError("node: NODE_OPTIONS must be empty for identity-only execution")
    lookup = shutil.which("node")
    if not lookup:
        raise TransportIdentityError("node: PATH does not resolve an executable")
    before = _file_identity(lookup, "node")
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        result = subprocess.run(["node", "-e", _NODE_RESOLVER, str(launcher)],
            capture_output=True, text=True, encoding="utf-8", errors="strict", timeout=15, **options)
        if result.returncode or len(result.stdout) > 32768:
            raise ValueError()
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", value.get("node_version", "")):
            raise ValueError()
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        # Never include subprocess stderr, argv representations, or raw JSON.
        raise TransportIdentityError("node: metadata-only resolver failed") from None
    actual = _file_identity(value.get("node_path"), "node")
    if actual != before or _file_identity(lookup, "node") != before:
        raise TransportIdentityError("node: PATH resolution or executable changed during capture")
    return {**value, "node_file": actual}


def transport_identity():
    """Return stable JSON data for gate, manifest, launch, and episode checks."""
    endpoints = _endpoints()  # reject unsafe URLs before executing even Node
    launcher = _file_identity(original.CODEX_JS, "codex.launcher")
    probe = _node_identity_probe(launcher["path"])
    launcher_package = _package_identity(Path(launcher["path"]).parent.parent / "package.json", "codex.package")
    native = _file_identity(probe.get("native_path"), "codex.native")
    native_package = (_package_identity(probe["native_package_json"], "codex.native_package")
                      if probe.get("native_package_json") else None)
    if _file_identity(original.CODEX_JS, "codex.launcher") != launcher:
        raise TransportIdentityError("codex.launcher: file changed during capture")
    return {"protocol": PROTOCOL, "endpoints": endpoints,
        "transport_source_sha256": _file_identity(original.__file__, "transport_source")["sha256"],
        "node": {**probe["node_file"], "version": probe["node_version"], "platform": probe["platform"], "arch": probe["arch"]},
        "codex": {"configured_launcher_path": str(original.CODEX_JS.absolute()), "launcher": launcher,
            "package": launcher_package, "native": native, "native_package": native_package,
            "native_package_specifier": probe["native_package_specifier"], "target_triple": probe["target_triple"],
            "resolution": "codex-js-createRequire-platform-package-then-vendor-v1"},
        "python": {**_file_identity(sys.executable, "python"), "version": sys.version,
                   "implementation": sys.implementation.name}}


def assert_transport_identity(expected):
    """Reject any external-runtime drift; diagnostics contain field names only."""
    if not isinstance(expected, dict) or expected.get("protocol") != PROTOCOL:
        raise TransportIdentityError("transport_identity: frozen identity is missing or incompatible")
    observed = transport_identity()
    if observed != expected:
        fields = [name for name in ("protocol", "endpoints", "transport_source_sha256", "node", "codex", "python")
                  if observed.get(name) != expected.get(name)]
        if set(observed) != set(expected):
            fields.append("schema")
        raise TransportIdentityError("transport identity changed: " + ", ".join(fields))
    return observed
