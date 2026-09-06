"""Copy the complete runtime into the execution checkout; never copy secrets."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
RUNTIME_PACKAGES = ("team_eval20260903", "team_runtime_v2", "swe_verified_20260903",
                    "swe_fixed_team_20260903", "minimal_value_20260905")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def runtime_sources(root=REPO):
    root = Path(root)
    paths = list((root / "modelbench").glob("*.py"))
    for name in RUNTIME_PACKAGES:
        paths.extend((root / "modelbench" / name).glob("*.py"))
    plugin = root / "dpswarm-plugin" / "dpswarm"
    for path in plugin.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix in (".py", ".json", ".yaml", ".yml", ".txt"):
            paths.append(path)
    paths.append(root / "modelbench" / "minimal_value_20260905" / "pricing.json")
    return {str(path.relative_to(root)).replace("\\", "/"): sha(path) for path in sorted(set(paths))}


def input_files(official):
    official = Path(official)
    required = ["versions.json", "selection.json", "selected_public.json", "verified.parquet",
                "grader_controller.json", "grader.Dockerfile", "resource_limits.json",
                "grader/test_specs_provenance.json", "grader/test_specs.json", "grader/selected.json",
                "public_checks.json"]
    optional = ["public_checks_provenance.json", "exposure.json", "qualification.json"]
    names = required + [n for n in optional if (official / n).is_file()]
    names += [path.relative_to(official).as_posix() for path in (official / "images").glob("*.json")]
    names += [path.relative_to(official).as_posix() for path in (official / "grader" / "preflight").rglob("qualification.json")]
    return {name: sha(official / name) for name in sorted(set(names))}


def build_snapshot(batch, gate, *, official=None):
    batch = Path(batch)
    destination = batch / "runtime_snapshot"
    if destination.exists():
        raise ValueError("Snapshot already exists; refusing to merge mutable runtimes")
    sources = runtime_sources()
    if gate.get("status") != "PASS" or gate.get("runtime_sources") != sources or not gate.get("live_run_admission"):
        raise ValueError("New gate does not admit these exact runtime sources")
    official = Path(official or HERE / "official")
    inputs = input_files(official)
    if gate.get("input_artifacts") != inputs:
        raise ValueError("New gate does not admit these exact inputs")
    destination.mkdir(parents=True)
    for relative, expected in sources.items():
        output = destination / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / relative, output)
        if sha(output) != expected:
            raise ValueError("Source changed during snapshot: " + relative)
    target_official = destination / "modelbench" / "minimal_value_20260905" / "official"
    for relative, expected in inputs.items():
        output = target_official / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(official / relative, output)
        if sha(output) != expected:
            raise ValueError("Input changed during snapshot: " + relative)
    # The legacy adapter hashes its own source; its data root is explicitly
    # redirected to the new official directory by ValueEnvironment.
    return {"root": str(destination.resolve()), "runtime_sources": sources,
            "input_artifacts": inputs, "created_at": datetime.now(timezone.utc).isoformat()}


def verify_snapshot(batch, manifest):
    batch = Path(batch)
    snapshot = batch / "runtime_snapshot"
    for relative, expected in manifest["runtime_sources"].items():
        path = snapshot / relative
        if not path.is_file() or sha(path) != expected:
            raise ValueError("Frozen runtime drift: " + relative)
    official = snapshot / "modelbench" / "minimal_value_20260905" / "official"
    for relative, expected in manifest["input_artifacts"].items():
        if sha(official / relative) != expected:
            raise ValueError("Frozen input drift: " + relative)
    if (snapshot / "modelbench" / "keys.local.json").exists():
        raise ValueError("Credentials must not be copied into the experiment snapshot")
    return snapshot


def credential_environment(source_repo=REPO):
    """Load local values into the child environment without recording values."""
    config = Path(source_repo) / "modelbench" / "keyconfig.py"
    spec = importlib.util.spec_from_file_location("_minimum_value_keyconfig", config)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    env = os.environ.copy()
    for key in ("GLM_API_KEY", "GLM_BASE_URL", "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"):
        value = module.get(key)
        if value:
            env[key] = value
    return env


def assert_import_origins(snapshot):
    snapshot = Path(snapshot).resolve()
    checked = {}
    for name, module in list(sys.modules.items()):
        if name == "dpswarm" or name.startswith("dpswarm.") or name == "modelbench" or name.startswith("modelbench."):
            path = getattr(module, "__file__", None)
            if path:
                resolved = Path(path).resolve()
                if not resolved.is_relative_to(snapshot):
                    raise ValueError("Runtime imported mutable source: " + name)
                checked[name] = {"path": str(resolved), "sha256": sha(resolved)}
    return checked


def launch_stage(batch):
    batch = Path(batch).resolve()
    manifest = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
    snapshot = verify_snapshot(batch, manifest)
    env = credential_environment()
    env["PYTHONPATH"] = os.pathsep.join((str(snapshot), str(snapshot / "dpswarm-plugin")))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    argv = [sys.executable, "-B", "-m", "modelbench.minimal_value_20260905.cli", "_run",
            "--batch", str(batch), "--snapshot-root", str(snapshot)]
    output = (batch / "controller.stdout.log").open("ab")
    errors = (batch / "controller.stderr.log").open("ab")
    try:
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        process = subprocess.Popen(argv, cwd=snapshot, env=env, stdout=output, stderr=errors, **options)
    finally:
        output.close()
        errors.close()
    # The saved launch descriptor contains no environment or credentials.
    descriptor = {"pid": process.pid, "argv": argv, "snapshot_root": str(snapshot),
                  "started_at": datetime.now(timezone.utc).isoformat()}
    (batch / "launch.json").write_text(json.dumps(descriptor, indent=2), encoding="utf-8")
    return descriptor
