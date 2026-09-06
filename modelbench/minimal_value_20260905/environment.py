"""Independent experiment adapter with explicit terminal workspace isolation.

Only this process redirects the historical implementation's OFFICIAL constant.
No historical data or source file is modified. Model tools receive candidate
methods; grading remains a separate host-only terminal operation.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import threading
import time

from modelbench.swe_verified_20260903 import environment as legacy

ROOT = Path(__file__).resolve().parent
OFFICIAL = ROOT / "official"
legacy.OFFICIAL = OFFICIAL
EnvironmentError = legacy.EnvironmentError
_GRADE_LOCK = threading.Lock()
_docker = legacy._docker
ensure_image = legacy.ensure_image
ensure_grader_specs = legacy.ensure_grader_specs
ensure_grader_controller = legacy.ensure_grader_controller
inspect_image = legacy.inspect_image
image_name = legacy.image_name


def capture_grader_contract():
    contract = legacy.capture_grader_contract()
    contract.update(scope="minimum_value_20260905_v1", adapter_environment_sha=legacy._file_sha(Path(__file__)),
                    adapter_protocol="stop-confirm-restart-maintenance-v1")
    for name in ("exposure.json", "public_checks.json", "public_checks_provenance.json"):
        if (OFFICIAL / name).exists():
            contract["input_artifacts"][name] = legacy._file_sha(OFFICIAL / name)
    return contract


def _verify_adapter(contract):
    if not contract or contract.get("adapter_protocol") != "stop-confirm-restart-maintenance-v1":
        raise EnvironmentError("independent adapter grading contract is required")
    if contract.get("adapter_environment_sha") != legacy._file_sha(Path(__file__)):
        raise EnvironmentError("frozen independent environment adapter changed")
    return legacy._verify_grader_contract(contract)


# Executed with isolated Python, with no conda activation or candidate shell.
# A separate temporary index prevents mutations of the repository's real index.
_GIT_SCRIPT = r'''
import json,os,subprocess,sys,tempfile
request=json.loads(sys.stdin.read())
env=dict(os.environ)
env.update(GIT_CONFIG_SYSTEM='/dev/null',GIT_CONFIG_GLOBAL='/dev/null',GIT_ATTR_NOSYSTEM='1',
           GIT_TERMINAL_PROMPT='0',GIT_WORK_TREE='/testbed',GIT_DIR='/testbed/.git')
git=['/usr/bin/git','-c','core.hooksPath=/dev/null','-c','core.fsmonitor=false',
     '-c','core.bare=false','-c','core.attributesFile=/dev/null','-c','diff.external=']
keys=subprocess.run(git+['config','--null','--name-only','--get-regexp',r'^filter\..*\.(clean|smudge|process|required)$'],
                    env=env,capture_output=True,text=True)
if keys.returncode not in (0,1):
    sys.stderr.write(keys.stderr);sys.exit(keys.returncode)
for key in keys.stdout.split('\0'):
    if key:
        git+=['-c',key+'='+('false' if key.endswith('.required') else '')]
fd,index=tempfile.mkstemp(prefix='minimum-value-index-',dir='/tmp');os.close(fd);os.unlink(index)
env['GIT_INDEX_FILE']=index
try:
    subprocess.run(git+['read-tree',request['baseline']],env=env,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if request['operation']=='export':
        subprocess.run(git+['add','-A','--','.'],env=env,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        result=subprocess.run(git+['diff','--cached','--binary','--no-ext-diff','--no-textconv',request['baseline'],'--'],
                              env=env,capture_output=True)
    else:
        patch=request['patch'].encode('utf-8')
        if not patch:
            sys.exit(0)
        result=subprocess.run(git+['apply','--check','--cached','--binary','-'],input=patch,env=env,capture_output=True)
    sys.stdout.buffer.write(result.stdout);sys.stderr.buffer.write(result.stderr);sys.exit(result.returncode)
finally:
    for path in (index,index+'.lock'):
        try:os.unlink(path)
        except FileNotFoundError:pass
'''


class ValueEnvironment(legacy.SWEEnvironment):
    def __init__(self, instance, run_dir, **kwargs):
        super().__init__(instance, run_dir, **kwargs)
        self._quiesced = False
        self._quiesce_record = None
        self._cleanup_record = None


    def start(self):
        with self._lock:
            if self._closed:
                raise EnvironmentError("environment has been closed")
            if not self.container_id:
                # Persist owner/name BEFORE Docker create; the outer watchdog can
                # reconcile this exact label-bound name after a process crash.
                legacy._json(self.run_dir / "container-intent.json", {
                    "owner": self.owner, "name": f"dpswarm-swe-{self.owner[:16]}",
                    "instance_id": self.instance_id, "scope": "minimum_value_candidate",
                })
            return super().start()


    def _owned_info(self):
        if not self.container_id:
            raise EnvironmentError("candidate container has not been created")
        result = _docker(["inspect", self.container_id], check=False)
        if result.returncode:
            raise EnvironmentError("cannot inspect owned candidate container")
        info = json.loads(result.stdout)[0]
        if info.get("Config", {}).get("Labels", {}).get("dpswarm.swe.owner") != self.owner:
            raise EnvironmentError("container ownership mismatch")
        return info

    def run(self, command, timeout=None):
        with self._lock:
            if self._quiesced:
                raise EnvironmentError("candidate execution is forbidden after quiesce")
            return super().run(command, timeout=timeout)

    def apply_patch(self, patch):
        with self._lock:
            if self._quiesced:
                raise EnvironmentError("candidate mutation is forbidden after quiesce")
            return super().apply_patch(patch)

    def quiesce(self, *, timeout=30):
        """Kill ALL candidate processes, verify stop, restart trusted maintenance.

        Docker exec can leave detached children after a shell exits. A confirmed
        container stop eliminates those writers before any terminal export.
        Restart creates only the original trusted tail init. The candidate API
        remains permanently disabled, and the same container slot stays held.
        """
        with self._lock:
            if self._quiesced:
                return dict(self._quiesce_record)
            if self._closed:
                raise EnvironmentError("closed environment cannot be quiesced/exported")
            before = self._owned_info()
            cid = self.container_id
            started = time.monotonic()
            _docker(["stop", "--time", str(max(1, min(10, int(timeout)))), cid], timeout=timeout)
            stopped = self._owned_info()
            state = stopped.get("State", {})
            if state.get("Running") is not False or state.get("Pid") != 0:
                raise EnvironmentError("candidate writers did not reach confirmed stopped state")
            # Fence candidate APIs before restarting; a failed restart stays fenced.
            self._quiesced = True
            self._quiesce_record = {"quiesced": True, "container_id": cid,
                "method": "docker-stop-confirm-restart-maintenance", "prior_pid": before.get("State", {}).get("Pid"),
                "stopped_running": False, "stopped_pid": 0, "maintenance_started": False,
                "candidate_execution_fenced": True, "duration_seconds": time.monotonic() - started}
            legacy._json(self.run_dir / "quiesce.json", self._quiesce_record)
            _docker(["start", cid], timeout=timeout)
            maintenance = self._owned_info()
            if maintenance.get("State", {}).get("Running") is not True:
                raise EnvironmentError("trusted export maintenance container failed to start")
            self._quiesce_record.update(maintenance_started=True, duration_seconds=time.monotonic() - started)
            legacy._json(self.run_dir / "quiesce.json", self._quiesce_record)
            return dict(self._quiesce_record)

    def _fixed_git(self, request):
        if not self.container_id or self._closed:
            raise EnvironmentError("environment is unavailable")
        result = _docker(["exec", "-i", "--user", "1000:1000", "--workdir", "/testbed", self.container_id,
            "/opt/miniconda3/bin/python", "-I", "-c", _GIT_SCRIPT],
            input=json.dumps(request), timeout=self.command_timeout + 15, check=False, output_limit=8 * 1024**2)
        if getattr(result, "output_limit_exceeded", False):
            self.close()
            raise EnvironmentError("trusted patch export exceeded 8 MiB")
        return result

    def snapshot_patch(self, delta=False):
        """Nonterminal observation only; this is not a frozen deliverable."""
        with self._lock:
            result = self._fixed_git({"operation": "export", "baseline": self.baseline_tree if delta else self.base_commit})
            if result.returncode:
                raise EnvironmentError(f"trusted patch snapshot failed: {result.stderr[-2000:]}")
            if len(result.stdout.encode("utf-8")) > 8 * 1024**2:
                raise EnvironmentError("patch exceeds 8 MiB")
            return result.stdout

    def export_patch(self, delta=False):
        with self._lock:
            if not self._quiesced or not self._quiesce_record.get("maintenance_started"):
                raise EnvironmentError("terminal export requires confirmed quiesce")
            patch = self.snapshot_patch(delta=delta)
            self.run_dir.mkdir(parents=True, exist_ok=True)
            (self.run_dir / ("delta.patch" if delta else "model.patch")).write_text(patch, encoding="utf-8", newline="\n")
            return patch

    def check_frozen_patch(self, patch):
        with self._lock:
            if not self._quiesced or not self._quiesce_record.get("maintenance_started"):
                raise EnvironmentError("frozen applicability requires confirmed quiesce")
            if not isinstance(patch, str):
                raise ValueError("patch must be text")
            result = self._fixed_git({"operation": "check", "baseline": self.base_commit, "patch": patch})
            evidence = {"applicable": result.returncode == 0,
                "patch_sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
                "baseline_commit": self.base_commit, "method": "temporary-index-git-apply-check-cached-binary",
                "exit_code": result.returncode, "stderr": result.stderr[-2000:]}
            legacy._json(self.run_dir / "patch_applicability.json", evidence)
            return evidence

    def fork(self, run_dir, *, baseline_patch=""):
        return type(self)(self.instance, run_dir, image=self.image, cpus=self.cpus, memory=self.memory,
            command_timeout=self.command_timeout, baseline_patch=baseline_patch, grader_contract=self.grader_contract)

    clone = fork

    def close(self):
        with self._lock:
            if self._closed and self._cleanup_record:
                return dict(self._cleanup_record)
            cid = self.container_id
            record = {"closed": False, "removed": False, "container_id": cid, "errors": []}
            try:
                if cid:
                    first = _docker(["inspect", cid], check=False)
                    if first.returncode == 0:
                        info = json.loads(first.stdout)[0]
                        if info.get("Config", {}).get("Labels", {}).get("dpswarm.swe.owner") != self.owner:
                            raise EnvironmentError("container ownership mismatch")
                        _docker(["rm", "-f", cid])
                        after = _docker(["inspect", cid], check=False)
                    else:
                        after = first
                    if after.returncode == 0 or not any(text in after.stderr.lower() for text in ("no such object", "no such container")):
                        raise EnvironmentError("owned candidate deletion was not confirmed")
                self.container_id = None
                self._closed = True
                record.update(closed=True, removed=bool(cid))
            except Exception as exc:
                record["errors"].append(str(exc))
                legacy._json(self.run_dir / "cleanup.json", record)
                self._cleanup_record = record
                raise
            legacy._json(self.run_dir / "cleanup.json", record)
            self._cleanup_record = record
            return dict(record)

    def grade(self, *args, **kwargs):
        raise EnvironmentError("candidate environment cannot grade; use host rootgrade_terminal")


def rootgrade_terminal(instance, frozen_patch_path, expected_sha256, run_dir, *, grader_contract,
                       image=None, model_name="candidate", timeout=900, cpus=2, memory="3g"):
    """Host-only terminal grader. Caller confirms all episode candidates closed."""
    patch_path = Path(frozen_patch_path)
    patch_bytes = patch_path.read_bytes()
    if hashlib.sha256(patch_bytes).hexdigest() != expected_sha256:
        raise EnvironmentError("frozen candidate patch hash mismatch")
    patch = patch_bytes.decode("utf-8")
    _verify_adapter(grader_contract)
    with _GRADE_LOCK:
        env = ValueEnvironment(instance, run_dir, image=image, cpus=cpus, memory=memory, grader_contract=grader_contract)
        result = legacy.SWEEnvironment.grade(env, patch, model_name=model_name, timeout=timeout)
    if patch_path.read_bytes() != patch_bytes:
        raise EnvironmentError("frozen candidate patch changed while grading")
    return {**result, "adapter_environment_sha": grader_contract["adapter_environment_sha"]}


grade_terminal = rootgrade_terminal
