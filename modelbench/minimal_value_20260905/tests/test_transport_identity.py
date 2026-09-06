import json
from pathlib import Path
import subprocess

import pytest

from modelbench.minimal_value_20260905 import transport_identity as module


@pytest.fixture
def installation(tmp_path, monkeypatch):
    paths = {
        "node": tmp_path / "node.exe",
        "launcher": tmp_path / "codex" / "bin" / "codex.js",
        "package": tmp_path / "codex" / "package.json",
        "native": tmp_path / "platform" / "vendor" / "target" / "bin" / "codex.exe",
        "native_package": tmp_path / "platform" / "package.json",
        "python": tmp_path / "python.exe",
        "source": tmp_path / "transports.py",
    }
    for name, path in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("test " + name).encode())
    paths["package"].write_text(json.dumps({"name": "@openai/codex", "version": "0.149.0"}))
    paths["native_package"].write_text(json.dumps({"name": "@openai/codex", "version": "0.149.0-win32-x64"}))
    values = {"GLM_BASE_URL": "https://glm.example/api/coding/v4/", "DEEPSEEK_BASE_URL": None}
    requested = []

    def lookup(name):
        requested.append(name)
        if name not in values:
            raise AssertionError("credential lookup is forbidden")
        return values[name]

    def probe(_launcher):
        return {"node_file": module._file_identity(paths["node"], "node"), "node_version": "v24.15.0",
            "platform": "win32", "arch": "x64", "native_path": str(paths["native"]),
            "native_package_json": str(paths["native_package"]),
            "native_package_specifier": "@openai/codex-win32-x64", "target_triple": "x86_64-pc-windows-msvc"}

    monkeypatch.setattr(module.original, "CODEX_JS", paths["launcher"])
    monkeypatch.setattr(module.original, "__file__", str(paths["source"]))
    monkeypatch.setattr(module.original, "_keyconfig", lookup)
    monkeypatch.setattr(module.sys, "executable", str(paths["python"]))
    monkeypatch.setattr(module, "_node_identity_probe", probe)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    return paths, values, requested


@pytest.mark.parametrize(("value", "expected"), [
    (" HTTPS://EXAMPLE.COM:443/api/coding/v4/// ", "https://example.com/api/coding/v4"),
    ("http://EXAMPLE.COM:80", "http://example.com"),
    ("https://EXAMPLE.COM:8443/Case/Path/", "https://example.com:8443/Case/Path"),
    ("http://[::1]:8080/api/", "http://[::1]:8080/api"),
])
def test_endpoint_normalization_preserves_route_path(value, expected):
    assert module.normalize_endpoint(value) == expected


@pytest.mark.parametrize("url", [
    "https://never-print-user:never-print-password@example.com/api",
    "https://example.com/api?api_key=never-print-query",
    "https://example.com/api#never-print-fragment",
    "https://example.com/api?",
    "https://example.com/api\nnever-print-control",
    "https://example.com:never-print-port/api",
])
def test_unsafe_endpoint_is_rejected_without_reflecting_its_value(url, capsys):
    with pytest.raises(module.TransportIdentityError) as failure:
        module.normalize_endpoint(url, field="endpoints.glm")
    assert "never-print" not in str(failure.value)
    assert url not in str(failure.value)
    assert capsys.readouterr().out == ""


def test_identity_is_stable_and_only_url_configuration_is_requested(installation):
    paths, _, requested = installation
    expected = module.transport_identity()
    assert module.assert_transport_identity(expected) == expected
    assert set(requested) == {"GLM_BASE_URL", "DEEPSEEK_BASE_URL"}
    assert expected["endpoints"]["deepseek"]["request_url"] == "https://api.deepseek.com/chat/completions"
    assert expected["codex"]["package"]["version"] == "0.149.0"
    assert expected["codex"]["native"]["path"] == str(paths["native"].resolve())
    assert json.loads(json.dumps(expected)) == expected


@pytest.mark.parametrize(("changed", "field"), [
    ("node", "node"), ("launcher", "codex"), ("native", "codex"),
    ("python", "python"), ("source", "transport_source_sha256"),
])
def test_changed_executable_or_source_rejects_reuse_of_the_same_manifest(installation, changed, field):
    paths, _, _ = installation
    expected = module.transport_identity()
    paths[changed].write_bytes(b"changed external executor")
    with pytest.raises(module.TransportIdentityError, match=field):
        module.assert_transport_identity(expected)


def test_npm_package_upgrade_is_detected_even_if_native_bytes_stay_the_same(installation):
    paths, _, _ = installation
    expected = module.transport_identity()
    paths["package"].write_text(json.dumps({"name": "@openai/codex", "version": "0.150.0"}))
    with pytest.raises(module.TransportIdentityError, match="codex"):
        module.assert_transport_identity(expected)


def test_endpoint_change_is_detected_without_printing_either_endpoint(installation):
    _, values, _ = installation
    expected = module.transport_identity()
    values["DEEPSEEK_BASE_URL"] = "https://other-provider.example/v1"
    with pytest.raises(module.TransportIdentityError, match="endpoints") as failure:
        module.assert_transport_identity(expected)
    assert ".example" not in str(failure.value) and "deepseek.com" not in str(failure.value)


def test_unsafe_endpoint_fails_before_node_can_execute(installation, monkeypatch):
    _, values, _ = installation
    values["GLM_BASE_URL"] = "https://example.com/coding/?key=never-print-secret"
    monkeypatch.setattr(module, "_node_identity_probe", lambda _: pytest.fail("Node must not start"))
    with pytest.raises(module.TransportIdentityError, match="not safe"):
        module.transport_identity()


def test_source_snapshot_location_does_not_change_external_identity(installation, monkeypatch, tmp_path):
    paths, _, _ = installation
    expected = module.transport_identity()
    copied_source = tmp_path / "runtime_snapshot" / "transports.py"
    copied_source.parent.mkdir()
    copied_source.write_bytes(paths["source"].read_bytes())
    monkeypatch.setattr(module.original, "__file__", str(copied_source))
    assert module.assert_transport_identity(expected) == expected


def test_node_preload_options_are_rejected_without_running_any_program(monkeypatch, tmp_path):
    monkeypatch.setenv("NODE_OPTIONS", "--require never-print-sensitive-preload")
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: pytest.fail("no process should run"))
    with pytest.raises(module.TransportIdentityError) as failure:
        module._node_identity_probe(tmp_path / "codex.js")
    assert "never-print" not in str(failure.value)


def test_node_resolver_failure_does_not_reflect_stderr(monkeypatch, tmp_path):
    node = tmp_path / "node.exe"
    node.write_bytes(b"node")
    monkeypatch.delenv("NODE_OPTIONS", raising=False)
    monkeypatch.setattr(module.shutil, "which", lambda _: str(node))
    def run(argv, **kwargs):
        assert argv[:2] == ["node", "-e"]
        assert "exec" not in argv
        return subprocess.CompletedProcess(argv, 1, "", "never-print-private-stderr")
    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(module.TransportIdentityError) as failure:
        module._node_identity_probe(tmp_path / "codex.js")
    assert "never-print" not in str(failure.value)


def test_missing_or_incompatible_identity_does_not_start_capture(monkeypatch):
    monkeypatch.setattr(module, "transport_identity", lambda: pytest.fail("must not run"))
    for expected in (None, {}, {"protocol": "another_protocol"}):
        with pytest.raises(module.TransportIdentityError, match="incompatible"):
            module.assert_transport_identity(expected)
