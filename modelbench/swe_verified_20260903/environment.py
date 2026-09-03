"""Isolated candidate workspaces and an out-of-process official SWE-bench grader.

The candidate receives no host mounts, credentials, dataset, gold patch, or
grading tests. Only a final, frozen diff crosses into a fresh grading container.
The caller owns the global container semaphore (including grader admission).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parent
OFFICIAL = ROOT / "official"
HARNESS_REVISION = "726c5461e2ef52d83cf1ea2107870a8bb3328d57"
CONTROLLER_IMAGE = "dpswarm-swe-grader:20260903"
GRADER_INPUTS = ("versions.json", "selection.json", "selected_public.json", "verified.parquet",
                "grader_controller.json", "grader.Dockerfile", "resource_limits.json",
                "grader/test_specs_provenance.json", "grader/test_specs.json", "grader/selected.json")
_LOCK = threading.RLock()
_IMAGE_LOCKS: dict[str, threading.Lock] = {}
FORBIDDEN_FIELDS = {"patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS", "hints_text", "difficulty", "eval_script"}


class EnvironmentError(RuntimeError):
    pass


def capture_grader_contract() -> dict:
    """Explicit preflight snapshot. Formal runs pass their immutable manifest."""
    return {"scope": "preflight", "input_artifacts": {name: _file_sha(OFFICIAL / name) for name in GRADER_INPUTS},
            "environment_sha": _file_sha(Path(__file__).resolve())}


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify_grader_contract(contract: dict | None) -> dict:
    if not contract:
        raise EnvironmentError("grading requires a frozen grader_contract; preflight uses capture_grader_contract()")
    artifacts = contract.get("input_artifacts", {})
    if not set(GRADER_INPUTS).issubset(artifacts):
        raise EnvironmentError("grader contract lacks required input fingerprints")
    for name, expected in artifacts.items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or not re.fullmatch(r"[a-zA-Z0-9_./-]+", name):
            raise EnvironmentError("invalid grader artifact path")
        if _file_sha(OFFICIAL / relative) != expected:
            raise EnvironmentError(f"frozen grader artifact changed: {name}")
    if _file_sha(Path(__file__).resolve()) != contract.get("environment_sha"):
        raise EnvironmentError("frozen environment source changed")
    versions = json.loads((OFFICIAL / "versions.json").read_text(encoding="utf-8"))
    controller = json.loads((OFFICIAL / "grader_controller.json").read_text(encoding="utf-8"))
    provenance = json.loads((OFFICIAL / "grader" / "test_specs_provenance.json").read_text(encoding="utf-8"))
    if versions["dataset_sha256"] != artifacts["verified.parquet"]:
        raise EnvironmentError("dataset bytes do not match version declaration")
    expected_provenance = {"sha256": artifacts["grader/test_specs.json"],
        "selected_sha256": artifacts["grader/selected.json"], "dataset_revision": versions["dataset_revision"],
        "dataset_sha256": versions["dataset_sha256"], "controller_image_id": controller["image_id"],
        "harness_commit": HARNESS_REVISION}
    if any(provenance.get(k) != v for k, v in expected_provenance.items()):
        raise EnvironmentError("TestSpec provenance does not match frozen dataset/controller")
    return {"scope": contract.get("scope", "experiment"), "input_artifacts": dict(artifacts),
            "environment_sha": contract["environment_sha"], "dataset_revision": versions["dataset_revision"],
            "dataset_sha256": versions["dataset_sha256"], "selected_sha256": artifacts["grader/selected.json"],
            "test_specs_sha256": artifacts["grader/test_specs.json"], "controller_image_id": controller["image_id"],
            "harness_commit": HARNESS_REVISION}


def _json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _docker(args: list[str], *, timeout=120, input=None, check=True, output_limit=None):
    if output_limit is None:
        result = subprocess.run(["docker", *args], input=input, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=timeout)
    else:
        proc = subprocess.Popen(["docker", *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        captured = {"stdout": bytearray(), "stderr": bytearray()}
        exceeded = threading.Event()
        def drain(name, stream):
            while chunk := stream.read(65536):
                remaining = max(0, output_limit - len(captured[name]))
                captured[name].extend(chunk[:remaining])
                if len(chunk) > remaining:
                    exceeded.set()
                    try:
                        proc.kill()
                    except OSError:
                        pass
            stream.close()
        readers = [threading.Thread(target=drain, args=(name, stream), daemon=True)
                   for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr))]
        for reader in readers:
            reader.start()
        try:
            if input is not None:
                try:
                    proc.stdin.write(input.encode("utf-8"))
                except BrokenPipeError:
                    pass
            try:
                proc.stdin.close()
            except BrokenPipeError:
                pass
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            for reader in readers:
                reader.join()
            raise subprocess.TimeoutExpired(["docker", *args], timeout,
                output=bytes(captured["stdout"]), stderr=bytes(captured["stderr"]))
        finally:
            for reader in readers:
                reader.join(timeout=5)
        result = subprocess.CompletedProcess(["docker", *args], proc.returncode,
            bytes(captured["stdout"]).decode("utf-8", errors="replace"),
            bytes(captured["stderr"]).decode("utf-8", errors="replace"))
        result.output_limit_exceeded = exceeded.is_set()
    if check and result.returncode:
        raise EnvironmentError(f"docker {args[0]} failed ({result.returncode}): {result.stderr[-4000:]}")
    return result


def image_name(instance_id: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+__[a-zA-Z0-9_.-]+-[0-9]+", instance_id):
        raise ValueError("invalid SWE-bench instance ID")
    return f"swebench/sweb.eval.x86_64.{instance_id.lower().replace('__', '_1776_')}:latest"


def inspect_image(image: str) -> dict | None:
    result = _docker(["image", "inspect", image], check=False)
    return json.loads(result.stdout)[0] if result.returncode == 0 else None


def ensure_grader_controller() -> dict:
    """Build the trusted Linux orchestration process without patching upstream."""
    record_path = OFFICIAL / "grader_controller.json"
    prior = json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() else None
    info = inspect_image(CONTROLLER_IMAGE)
    if not info:
        with (OFFICIAL / "grader_controller.build.log").open("w", encoding="utf-8") as log:
            proc = subprocess.run(["docker", "build", "-t", CONTROLLER_IMAGE, "-f",
                                   str(OFFICIAL / "grader.Dockerfile"), str(OFFICIAL / "SWE-bench")],
                                  stdout=log, stderr=subprocess.STDOUT, timeout=1800)
        if proc.returncode:
            raise EnvironmentError("grader controller build failed; see official/grader_controller.build.log")
        info = inspect_image(CONTROLLER_IMAGE)
    if not info or (prior and prior["image_id"] != info["Id"]):
        raise EnvironmentError("grader controller image is absent or changed")
    record = {"image": CONTROLLER_IMAGE, "image_id": info["Id"], "harness_commit": HARNESS_REVISION,
              "dockerfile_sha256": hashlib.sha256((OFFICIAL / "grader.Dockerfile").read_bytes()).hexdigest(),
              "memory": "768m", "cpus": 1, "trusted_socket_access": True}
    _json(record_path, record)
    return record


def ensure_grader_specs() -> dict:
    """Materialize official TestSpecs once; setup may fetch commit-pinned requirements.

    This trusted preparation process has network access but no Docker socket.
    The subsequent controller and testcase both run with network=none.
    """
    path = OFFICIAL / "grader" / "test_specs.json"
    provenance = OFFICIAL / "grader" / "test_specs_provenance.json"
    controller = ensure_grader_controller()
    versions = json.loads((OFFICIAL / "versions.json").read_text(encoding="utf-8"))
    source_binding = {"selected_sha256": _file_sha(OFFICIAL / "grader" / "selected.json"),
        "dataset_revision": versions["dataset_revision"], "dataset_sha256": versions["dataset_sha256"],
        "controller_image_id": controller["image_id"], "harness_commit": HARNESS_REVISION}
    if path.exists() and provenance.exists():
        prior = json.loads(provenance.read_text(encoding="utf-8"))
        if prior["sha256"] != hashlib.sha256(path.read_bytes()).hexdigest():
            raise EnvironmentError("official TestSpec snapshot changed")
        if any(prior.get(k) != v for k, v in source_binding.items()):
            raise EnvironmentError("TestSpec provenance/source binding mismatch")
        return prior
    script = """import json,dataclasses,hashlib
from pathlib import Path
from swebench.harness.test_spec.test_spec import make_test_spec
rows=json.loads(Path('/data/selected.json').read_text())
specs={r['instance_id']:dataclasses.asdict(make_test_spec(r,namespace='swebench')) for r in rows}
target=Path('/data/test_specs.json')
target.write_text(json.dumps(specs,ensure_ascii=False,indent=2))
print(json.dumps({'prepared_ids':list(specs),'sha256':hashlib.sha256(target.read_bytes()).hexdigest()}))
"""
    with (OFFICIAL / "grader" / "test_specs.prepare.log").open("w", encoding="utf-8") as log:
        proc = subprocess.run(["docker", "run", "--rm", "--network", "bridge", "--memory", "768m",
            "--cpus", "1", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--mount", f"type=bind,source={OFFICIAL / 'grader'},target=/data",
            controller["image_id"], "-c", script], stdout=log, stderr=subprocess.STDOUT, timeout=600)
    if proc.returncode or not path.exists():
        raise EnvironmentError("official TestSpec preparation failed; inspect test_specs.prepare.log")
    record = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), **source_binding, "preparation_network": "bridge",
              "grading_network": "none", "source": "unmodified official make_test_spec"}
    _json(provenance, record)
    return record


def ensure_image(instance_id: str) -> dict:
    """Pull only the requested frozen instance; persist digest and pull output."""
    image = image_name(instance_id)
    path = OFFICIAL / "images" / f"{instance_id}.json"
    with _LOCK:
        lock = _IMAGE_LOCKS.setdefault(image, threading.Lock())
    with lock:
        prior = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        info = inspect_image(image)
        preexisting = info is not None
        before = OFFICIAL / "images_before.jsonl"
        if info and before.exists():
            saved = [json.loads(line) for line in before.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
            preexisting = any(row.get("ID") == info["Id"] for row in saved)
        if info is None:
            # Docker Desktop's backing disk is commonly on C:. Do not exhaust it.
            if os.name == "nt" and shutil.disk_usage("C:\\").free < 12 * 1024**3:
                raise EnvironmentError("less than 12 GiB free on C:; image pull not admitted")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.with_suffix(".pull.log").open("a", encoding="utf-8") as log:
                result = subprocess.run(["docker", "pull", image], stdout=log, stderr=subprocess.STDOUT, timeout=1800)
            if result.returncode:
                _json(path, {"instance_id": instance_id, "image": image, "pull_failed": True, "preexisting": False})
                raise EnvironmentError(f"image pull failed; see {path.with_suffix('.pull.log')}")
            info = inspect_image(image)
        if info is None:
            raise EnvironmentError("image is absent after pull")
        if prior and prior.get("image_id") and prior["image_id"] != info["Id"]:
            raise EnvironmentError("image digest changed after freezing")
        record = {"instance_id": instance_id, "image": image, "image_id": info["Id"],
                  "repo_digests": info.get("RepoDigests", []), "size_bytes": info["Size"],
                  "preexisting": prior.get("preexisting", preexisting) if prior else preexisting}
        _json(path, record)
        return record


def cleanup_image(instance_id: str) -> dict:
    """Delete this round's added instance image only, never preexisting images."""
    path = OFFICIAL / "images" / f"{instance_id}.json"
    if not path.exists():
        return {"removed": False, "reason": "not recorded"}
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("preexisting", True):
        return {"removed": False, "reason": "preexisting"}
    image = record["image"]
    info = inspect_image(image)
    if not info:
        return {"removed": False, "reason": "already absent"}
    if info["Id"] != record.get("image_id"):
        raise EnvironmentError("refusing to remove changed image")
    references = _docker(["ps", "-aq", "--filter", f"ancestor={info['Id']}"]).stdout.strip()
    if references:
        return {"removed": False, "reason": "container references exist"}
    result = _docker(["image", "rm", image], check=False)
    return {"removed": result.returncode == 0, "stdout": result.stdout, "stderr": result.stderr}


class SWEEnvironment:
    def __init__(self, instance: dict, run_dir: Path, *, image: str | None = None,
                 cpus: float = 2, memory: str = "3g", command_timeout: int = 120,
                 baseline_patch: str = "", grader_contract: dict | None = None):
        if FORBIDDEN_FIELDS.intersection(instance):
            raise ValueError("candidate instance contains private grading/answer fields")
        self.instance = dict(instance)
        self.instance_id = instance["instance_id"]
        image_name(self.instance_id)
        self.base_commit = instance["base_commit"]
        if not re.fullmatch(r"[0-9a-f]{40}", self.base_commit):
            raise ValueError("base_commit must be a full commit hash")
        if cpus <= 0 or not re.fullmatch(r"[1-9][0-9]*(?:[mgMG])", memory):
            raise ValueError("invalid container resource limit")
        self.run_dir = Path(run_dir).resolve()
        self.image = image or image_name(self.instance_id)
        self.cpus, self.memory, self.command_timeout = cpus, memory, command_timeout
        self.baseline_patch = baseline_patch
        self.grader_contract = json.loads(json.dumps(grader_contract)) if grader_contract else None
        self.baseline_tree = self.base_commit
        self.container_id: str | None = None
        self.image_id: str | None = None
        self.owner = uuid.uuid4().hex
        self._closed = False
        self._lock = threading.RLock()
        self._command_seq = 0

    def start(self):
        with self._lock:
            if self.container_id:
                return self
            if self._closed:
                raise EnvironmentError("environment has been closed")
            self.run_dir.mkdir(parents=True, exist_ok=True)
            info = ensure_image(self.instance_id) if self.image == image_name(self.instance_id) else inspect_image(self.image)
            if not info:
                raise EnvironmentError("configured image is not installed")
            self.image_id = info.get("image_id", info.get("Id"))
            result = _docker(["create", "--name", f"dpswarm-swe-{self.owner[:16]}",
                "--label", f"dpswarm.swe.owner={self.owner}", "--network", "none",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "512",
                "--memory", self.memory, "--memory-swap", self.memory, "--cpus", str(self.cpus),
                "--user", "0:0", "--workdir", "/testbed", "--env", "HOME=/tmp/agent-home",
                "--env", "GIT_CONFIG_COUNT=1", "--env", "GIT_CONFIG_KEY_0=safe.directory",
                "--env", "GIT_CONFIG_VALUE_0=/testbed", "--entrypoint", "/bin/bash",
                self.image_id, "-c", "exec tail -f /dev/null"])
            self.container_id = result.stdout.strip()
            _json(self.run_dir / "container.json", {"id": self.container_id, "owner": self.owner,
                "image_id": self.image_id, "instance_id": self.instance_id, "base_commit": self.base_commit,
                "cpus": self.cpus, "memory": self.memory, "network": "none"})
            try:
                _docker(["start", self.container_id])
                # Retain the exact official base and its ancestors, remove future refs/reflogs.
                script = f"""set -eu
cd /testbed
git checkout --detach --force {self.base_commit}
git clean -fd
test "$(git rev-parse HEAD)" = {self.base_commit}
git for-each-ref --format='%(refname)' | while read -r ref; do git update-ref -d "$ref"; done
git reflog expire --expire=now --all
git gc --prune=now
mkdir -p /tmp/agent-home
chmod 777 /tmp/agent-home
"""
                self._exec(script, user="0:0", timeout=300, check=True)
                self._prepare_workspace_permissions()
                if self.baseline_patch:
                    self.apply_patch(self.baseline_patch)
                self.baseline_tree = self._tree()
                if self.baseline_patch:
                    # A reachable local baseline commit preserves the delegation
                    # snapshot through ordinary git maintenance in the child.
                    commit = self._exec(f"git -c user.name=DPswarm -c user.email=benchmark@localhost "
                        f"commit-tree {self.baseline_tree} -p {self.base_commit} -m 'Delegation baseline'", check=True)["stdout"].strip()
                    self._exec(f"git update-ref refs/heads/dpswarm-baseline {commit}; git reset --soft {commit}", check=True)
                _json(self.run_dir / "baseline.json", {"base_commit": self.base_commit,
                    "baseline_tree": self.baseline_tree, "baseline_patch_sha256": _sha(self.baseline_patch)})
            except BaseException:
                self.close()
                raise
            return self

    def _prepare_workspace_permissions(self):
        """Trusted setup uses each inode owner's ordinary chmod permission.

        Official images can contain build files owned by non-root UIDs. With
        every capability dropped, UID 0 cannot chmod those files. The host may
        select an exec UID before any candidate runs; no process gains FOWNER,
        mounts, network or extra privilege. Do not follow repository symlinks.
        """
        discovered = self._exec("set -euo pipefail; find /testbed -xdev ! -type l -printf '%U\\n' | sort -nu",
                                user="0:0", timeout=300, check=True)["stdout"].splitlines()
        if not discovered or any(not re.fullmatch(r"[0-9]+", uid) or int(uid) >= 2**32 - 1
                                 for uid in discovered):
            raise EnvironmentError("workspace owner discovery returned invalid UIDs")
        owners = sorted({int(uid) for uid in discovered})
        for uid in owners:
            self._exec(f"set -eu; find /testbed -xdev -uid {uid} ! -type l -exec chmod a+rwX -- {{}} +",
                       user=f"{uid}:{uid}", timeout=300, check=True)
        _json(self.run_dir / "permissions.json", {"strategy": "owner-scoped-chmod-v1", "owner_uids": owners,
            "candidate_user": "1000:1000", "follow_symlinks": False, "added_capabilities": []})

    def _exec(self, command: str, *, user="1000:1000", timeout=None, input=None, check=False):
        if not self.container_id or self._closed:
            raise EnvironmentError("environment is not running")
        timeout = int(timeout or self.command_timeout)
        if timeout < 1:
            raise ValueError("timeout must be positive")
        activation = "if [ -f /opt/miniconda3/bin/activate ]; then . /opt/miniconda3/bin/activate; conda activate testbed; fi\n"
        args = ["exec", "-i", "--user", user, "--workdir", "/testbed", self.container_id,
                "timeout", "--signal=TERM", "--kill-after=5s", f"{timeout}s", "/bin/bash", "-c", activation + command]
        started = time.monotonic()
        try:
            proc = _docker(args, timeout=timeout + 15, input=input, check=False, output_limit=8 * 1024**2)
            result = {"stdout": proc.stdout, "stderr": proc.stderr, "exit_code": proc.returncode,
                      "timed_out": proc.returncode in (124, 137), "duration_seconds": time.monotonic() - started}
            if getattr(proc, "output_limit_exceeded", False):
                self.close()
                result.update(exit_code=125, output_limit_exceeded=True)
                result["stderr"] += "\nCommand output exceeded 8 MiB per stream; owned container terminated."
        except subprocess.TimeoutExpired as exc:
            # Kill this owned container, so a timed-out command cannot mutate later output.
            self.close()
            result = {"stdout": _text(exc.stdout), "stderr": _text(exc.stderr), "exit_code": 124,
                      "timed_out": True, "duration_seconds": time.monotonic() - started}
        if check and result["exit_code"]:
            raise EnvironmentError(f"container command failed: {result['stderr'][-3000:]}")
        return result

    def run(self, command: str, timeout: int | None = None) -> dict:
        with self._lock:
            if not isinstance(command, str) or not command.strip():
                raise ValueError("command must be nonempty text")
            self._command_seq += 1
            result = self._exec(command, timeout=timeout)
            if result["timed_out"]:
                self.close()
            _json(self.run_dir / "commands" / f"{self._command_seq:05d}.json", {"command": command, **result})
            # Full output remains in the host-only command record.
            return {**result, "stdout": result["stdout"][-40000:], "stderr": result["stderr"][-12000:]}

    def _tree(self) -> str:
        index = f"/tmp/dpswarm-index-{uuid.uuid4().hex}"
        script = f"export GIT_INDEX_FILE={index}; trap 'rm -f {index}' EXIT; git read-tree {self.base_commit}; git add -A; git write-tree"
        return self._exec(script, check=True)["stdout"].strip().splitlines()[-1]

    def export_patch(self, delta: bool = False) -> str:
        with self._lock:
            baseline = self.baseline_tree if delta else self.base_commit
            index = f"/tmp/dpswarm-index-{uuid.uuid4().hex}"
            script = (f"set -e; export GIT_INDEX_FILE={index}; trap 'rm -f {index}' EXIT; "
                      f"git read-tree {baseline}; git add -A; "
                      f"git -c core.fileMode=false diff --cached --binary --no-ext-diff {baseline} --")
            patch = self._exec(script, timeout=120, check=True)["stdout"]
            if len(patch.encode()) > 8 * 1024**2:
                raise EnvironmentError("patch exceeds 8 MiB")
            (self.run_dir / ("delta.patch" if delta else "model.patch")).write_text(patch, encoding="utf-8", newline="\n")
            return patch

    def apply_patch(self, patch: str) -> dict:
        with self._lock:
            if not isinstance(patch, str) or len(patch.encode()) > 8 * 1024**2:
                raise ValueError("invalid patch")
            if not patch:
                return {"stdout": "", "stderr": "", "exit_code": 0, "timed_out": False}
            filename = f"/tmp/dpswarm-patch-{uuid.uuid4().hex}"
            script = f"set -e; trap 'rm -f {filename}' EXIT; cat > {filename}; git apply --check --binary {filename}; git apply --binary {filename}"
            return self._exec(script, input=patch, check=True)

    def fork(self, run_dir: Path, *, baseline_patch: str) -> "SWEEnvironment":
        """The caller snapshots the lead before scheduling; no later lead reads."""
        return SWEEnvironment(self.instance, run_dir, image=self.image, cpus=self.cpus,
                              memory=self.memory, command_timeout=self.command_timeout,
                              baseline_patch=baseline_patch, grader_contract=self.grader_contract).start()

    def grade(self, patch: str | None = None, *, model_name="candidate", timeout=900) -> dict:
        """Host-only terminal action. Caller releases candidate slots before grading."""
        if patch is None:
            patch = self.export_patch()
        if not isinstance(patch, str):
            raise ValueError("patch must be text")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        patch_path = self.run_dir / "final.patch"
        if patch_path.exists() and patch_path.read_text(encoding="utf-8") != patch:
            raise EnvironmentError("final patch is already frozen to different bytes")
        patch_path.write_text(patch, encoding="utf-8", newline="\n")
        self.close()
        job = self.run_dir / "grader"
        job.mkdir(exist_ok=True)
        result_path = job / "result.json"
        binding = _verify_grader_contract(self.grader_contract)
        if result_path.exists():
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if result.get("patch_sha256") != _sha(patch):
                raise EnvironmentError("graded patch hash mismatch")
            if result.get("binding") != binding:
                raise EnvironmentError("cached grading contract mismatch")
            if result.get("instance_id") != self.instance_id or result.get("base_commit") != self.base_commit:
                raise EnvironmentError("cached grading instance mismatch")
            return {**result, "grader_dir": str(job)}
        old_request_path = job / "request.json"
        if old_request_path.exists():
            old_owner = json.loads(old_request_path.read_text(encoding="utf-8"))["owner"]
            errors = _cleanup_recorded(job / "controller.json", old_owner) + _cleanup_recorded(job / "containers.json", old_owner)
            if errors:
                raise EnvironmentError(f"previous grading resources could not be reconciled: {errors}")
        image = inspect_image(self.image)
        if not image:
            raise EnvironmentError("grading image is not installed")
        if self.image_id and self.image_id != image["Id"]:
            raise EnvironmentError("grading image changed since candidate startup")
        controller = ensure_grader_controller()
        ensure_grader_specs()
        if controller["image_id"] != binding["controller_image_id"]:
            raise EnvironmentError("controller image does not match frozen grading contract")
        request = {"instance_id": self.instance_id, "base_commit": self.base_commit,
                   "image_id": image["Id"], "patch_path": str(patch_path), "patch_sha256": _sha(patch),
                   "model_name": re.sub(r"[^a-zA-Z0-9_.-]", "_", model_name), "timeout": timeout,
                   "memory": self.memory, "cpus": self.cpus, "owner": uuid.uuid4().hex,
                   "grader_contract": self.grader_contract, "binding": binding}
        _json(job / "request.json", request)
        shutil.copyfile(patch_path, job / "model.patch")
        controller_request = {**request, "patch_path": "/job/model.patch", "controller_image_id": controller["image_id"]}
        _json(job / "controller_request.json", controller_request)
        controller_name = f"dpswarm-swe-grader-{request['owner'][:16]}"
        # Persist the exact owned names before Docker creation, closing the
        # create/record crash window without broad container enumeration.
        _json(job / "controller.json", [controller_name])
        _json(job / "containers.json", [f"sweb.eval.{self.instance_id.lower()}.{request['owner']}"])
        mounts = []
        for name in binding["input_artifacts"]:
            mounts += ["--mount", f"type=bind,source={OFFICIAL / name},target=/bridge/official/{name},readonly"]
        controller_id = _docker(["create", "--name", controller_name,
            "--label", f"dpswarm.swe.owner={request['owner']}", "--network", "none", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--memory", "768m", "--memory-swap", "768m",
            "--cpus", "1", "--pids-limit", "256", "--workdir", "/job",
            "--mount", f"type=bind,source={job},target=/job",
            "--mount", f"type=bind,source={Path(__file__).resolve()},target=/bridge/environment.py,readonly",
            *mounts,
            "--mount", "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
            "--env", "DPSWARM_HARNESS_REPO=/opt/SWE-bench", controller["image_id"],
            "/bridge/environment.py", "_grade", "/job/controller_request.json"]).stdout.strip()
        _json(job / "controller.json", [controller_id])
        with (job / "harness.stdout.log").open("w", encoding="utf-8") as out, (job / "harness.stderr.log").open("w", encoding="utf-8") as err:
            try:
                proc = subprocess.run(["docker", "start", "--attach", controller_id],
                                      cwd=job, stdout=out, stderr=err, timeout=timeout + 180,
                                      env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
            except subprocess.TimeoutExpired:
                proc = None
            finally:
                cleanup_errors = (_cleanup_recorded(job / "controller.json", request["owner"])
                                  + _cleanup_recorded(job / "containers.json", request["owner"]))
        if not result_path.exists():
            result = {"completed": False, "resolved": None, "infrastructure_error": "official harness did not produce a result",
                      "process_exit_code": None if proc is None else proc.returncode, "patch_sha256": _sha(patch)}
            _json(result_path, result)
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if result.get("completed") and result.get("binding") != binding:
            result.update(completed=False, resolved=None, infrastructure_error="controller result binding mismatch")
        result["binding"] = binding
        result["instance_id"] = self.instance_id
        result["base_commit"] = self.base_commit
        result["process_exit_code"] = None if proc is None else proc.returncode
        result["cleanup_errors"] = cleanup_errors
        if cleanup_errors or proc is None or proc.returncode != 0:
            result["official_completed"] = result.get("completed")
            result["official_resolved"] = result.get("resolved")
            result.update(completed=False, resolved=None,
                infrastructure_error="grading process or owned-resource cleanup failed")
        _json(result_path, result)
        result["grader_dir"] = str(job)
        return result

    def close(self):
        with self._lock:
            if self.container_id:
                result = _docker(["inspect", self.container_id], check=False)
                if result.returncode == 0:
                    info = json.loads(result.stdout)[0]
                    if info.get("Config", {}).get("Labels", {}).get("dpswarm.swe.owner") != self.owner:
                        raise EnvironmentError("container ownership mismatch")
                    _docker(["rm", "-f", self.container_id])
                elif "no such object" not in result.stderr.lower() and "no such container" not in result.stderr.lower():
                    raise EnvironmentError(f"cannot verify owned container cleanup: {result.stderr[-1000:]}")
                self.container_id = None
            self._closed = True

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _text(value) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else (value or "")


def _classify_official_status(status: dict | None, patch: str) -> dict:
    result = dict(status or {"completed": False, "resolved": None})
    result["official_status"] = status
    if not result.get("completed"):
        if not patch.strip():
            result.update(failure_kind="candidate_empty_patch", resolved=False,
                          official_completed=result.get("completed"),
                          official_resolved=(status or {}).get("resolved"))
        else:
            result.update(resolved=None, infrastructure_error="official evaluation did not complete; inspect harness logs")
    return result


def _cleanup_recorded(path: Path, owner: str):
    if not path.exists():
        return []
    errors = []
    for container_id in json.loads(path.read_text(encoding="utf-8")):
        result = _docker(["inspect", container_id], check=False)
        if result.returncode == 0:
            info = json.loads(result.stdout)[0]
            if info.get("Config", {}).get("Labels", {}).get("dpswarm.swe.owner") == owner:
                removed = _docker(["rm", "-f", container_id], check=False)
                if removed.returncode:
                    errors.append(f"remove {container_id}: {removed.stderr[-1000:]}")
            else:
                errors.append(f"ownership mismatch: {container_id}")
        elif "no such object" not in result.stderr.lower() and "no such container" not in result.stderr.lower():
            errors.append(f"cannot verify {container_id}: {result.stderr[-1000:]}")
    return errors


def _grade(request_path: Path):
    """Trusted subprocess: official implementation and unmodified grading tests."""
    request = json.loads(request_path.read_text(encoding="utf-8"))
    binding = _verify_grader_contract(request["grader_contract"])
    if binding != request["binding"] or binding["controller_image_id"] != request["controller_image_id"]:
        raise EnvironmentError("controller request binding mismatch")
    import docker
    from swebench.harness.run_evaluation import run_instance
    from swebench.harness.test_spec.test_spec import TestSpec
    harness_repo = Path(os.environ.get("DPSWARM_HARNESS_REPO", str(OFFICIAL / "SWE-bench")))
    revision = subprocess.check_output(["git", "-C", str(harness_repo), "rev-parse", "HEAD"], text=True).strip()
    if revision != HARNESS_REVISION:
        raise EnvironmentError("official harness revision changed")
    imported_file = Path(sys.modules[run_instance.__module__].__file__)
    imported_sha = _file_sha(imported_file)
    if imported_sha != _file_sha(harness_repo / "swebench/harness/run_evaluation.py"):
        raise EnvironmentError("imported run_instance differs from official source in image")
    records = json.loads((OFFICIAL / "grader" / "selected.json").read_text(encoding="utf-8"))
    instance = next(r for r in records if r["instance_id"] == request["instance_id"])
    if instance["base_commit"] != request["base_commit"]:
        raise EnvironmentError("grading base_commit mismatch")
    patch = Path(request["patch_path"]).read_text(encoding="utf-8")
    if _sha(patch) != request["patch_sha256"]:
        raise EnvironmentError("frozen patch changed")
    frozen_specs = json.loads((OFFICIAL / "grader" / "test_specs.json").read_text(encoding="utf-8"))
    spec = TestSpec(**frozen_specs[request["instance_id"]])
    real_client = docker.from_env(timeout=180)
    if real_client.containers.get(os.uname().nodename).image.id != binding["controller_image_id"]:
        raise EnvironmentError("actual controller image differs from frozen contract")
    ids = []
    class Containers:
        def __getattr__(self, name):
            return getattr(real_client.containers, name)
        def create(self, **kwargs):
            # Resource/isolation policy only; official patch/test/grading code is unchanged.
            kwargs.update(image=request["image_id"], network_mode="none", cap_add=[], cap_drop=["ALL"],
                          security_opt=["no-new-privileges"], mem_limit=request["memory"],
                          memswap_limit=request["memory"], nano_cpus=int(request["cpus"] * 1_000_000_000),
                          pids_limit=512, labels={"dpswarm.swe.owner": request["owner"]})
            container = real_client.containers.create(**kwargs)
            ids.append(container.id)
            _json(request_path.parent / "containers.json", ids)
            return container
    class Client:
        containers = Containers()
        def __getattr__(self, name):
            return getattr(real_client, name)
    pred = {"instance_id": request["instance_id"], "model_patch": patch, "model_name_or_path": request["model_name"]}
    try:
        status = run_instance(spec, pred, rm_image=False, force_rebuild=False, client=Client(),
                              run_id=request["owner"], timeout=request["timeout"])
        reports = list(request_path.parent.glob("logs/run_evaluation/**/report.json"))
        result = {**_classify_official_status(status, patch), "patch_sha256": _sha(patch),
                  "harness_commit": revision, "image_id": request["image_id"],
                  "instance_id": request["instance_id"], "base_commit": request["base_commit"],
                  "imported_run_evaluation": str(imported_file), "imported_run_evaluation_sha256": imported_sha,
                  "controller_image_id": request.get("controller_image_id"), "binding": binding,
                  "reports": [str(p.relative_to(request_path.parent)) for p in reports],
                  "reports_sha256": {str(p.relative_to(request_path.parent)): _file_sha(p) for p in reports}}
        _json(request_path.parent / "result.json", result)
    finally:
        for container_id in ids:
            try:
                container = real_client.containers.get(container_id)
                if container.labels.get("dpswarm.swe.owner") == request["owner"]:
                    container.remove(force=True)
            except docker.errors.NotFound:
                pass
        real_client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["_grade"])
    parser.add_argument("request", type=Path)
    arguments = parser.parse_args()
    _grade(arguments.request.resolve())
