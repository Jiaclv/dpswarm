"""Docker-enforced role tools for the unmodified TeamBench AgentLoop.

No model-provided command, path, or file content is executed on the host.
Message delivery remains the official host-side SendMessageTool's responsibility.
The caller prepares a seed-specific task directory and run/workspace. Ground truth
belongs in run/grader/expected.json, never in role-visible reports.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
import uuid

DEFAULT_IMAGE = "dpswarm-teambench:20260903"
TOOL_TIMEOUT = 60
GRADE_TIMEOUT = 180
OUTPUT_LIMIT = 131072
ROLES = ("planner", "executor", "verifier", "oracle")

# Runs INSIDE a role container. A JSON stdin avoids quoting/injection of paths,
# file contents, and shell commands in host-side docker arguments.
_TOOL_SCRIPT = r'''
import json, os, signal, subprocess, sys, threading
role, name = sys.argv[1:3]
args = json.load(sys.stdin)
limit = 131072
workspace, reports, submission = '/shared/workspace', '/shared/reports', '/shared/submission'
roots = {
    'planner': ['/task/spec.md', '/task/brief.md'],
    'executor': [workspace, reports, '/task/brief.md'],
    'verifier': [workspace, reports, submission, '/task/spec.md', '/task/brief.md'],
    'oracle': [workspace, reports, submission, '/task/spec.md', '/task/brief.md'],
}
writable = {'planner': [], 'executor': [workspace, reports], 'verifier': [submission],
            'oracle': [workspace, reports, submission]}
def allowed(path, candidates):
    return any(path == root or (not root.startswith('/task/') and path.startswith(root + '/'))
               for root in candidates)
def resolve(path, writing=False):
    if not isinstance(path, str) or not path or '\x00' in path:
        raise ValueError('A nonempty file path is required')
    base = '/task' if role == 'planner' else (submission if writing and role == 'verifier' else workspace)
    candidate = os.path.realpath(path if os.path.isabs(path) else os.path.join(base, path))
    candidates = writable[role] if writing else roots[role]
    if not allowed(candidate, candidates):
        raise PermissionError('Path is outside this role\'s allowed roots')
    return candidate
def run(cmd):
    if role == 'planner':
        raise PermissionError('Planner cannot execute commands')
    if not isinstance(cmd, str) or not cmd.strip():
        raise ValueError('A nonempty cmd is required')
    proc = subprocess.Popen(['/bin/bash', '--noprofile', '--norc', '-c', cmd],
        cwd=workspace, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    streams = [bytearray(), bytearray()]
    counts = [0, 0]
    def drain(pipe, idx):
        while True:
            block = pipe.read(8192)
            if not block:
                return
            counts[idx] += len(block)
            streams[idx].extend(block[:max(0, limit-len(streams[idx]))])
    readers = [threading.Thread(target=drain, args=(pipe, idx), daemon=True)
               for idx, pipe in enumerate((proc.stdout, proc.stderr))]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        code = proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        timed_out, code = True, 124
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)
    for reader in readers:
        reader.join(timeout=2)
    output = [bytes(part).decode('utf-8', errors='replace') for part in streams]
    for idx in range(2):
        if counts[idx] > limit:
            output[idx] += '\n[output truncated at 131072 bytes]'
    if timed_out:
        output[1] += '\nCommand timed out after 60 seconds.'
    return {'stdout': output[0], 'stderr': output[1], 'exit_code': code}
try:
    if role not in roots:
        raise ValueError('Unknown role')
    if name == 'run':
        result = run(args.get('cmd'))
    elif name == 'read':
        path = resolve(args.get('path'))
        with open(path, 'rb') as file:
            data = file.read(limit+1)
        text = data[:limit].decode('utf-8', errors='replace')
        if len(data) > limit:
            text += '\n[output truncated at 131072 bytes]'
        result = {'stdout': text, 'stderr': '', 'exit_code': 0}
    elif name == 'write':
        path = resolve(args.get('path'), True)
        content = args.get('content')
        if not isinstance(content, str):
            raise ValueError('content must be a string')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        path = resolve(path, True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            file.write(content)
        result = {'stdout': 'Written %d bytes to %s' % (len(content.encode('utf-8')), args['path']),
                  'stderr': '', 'exit_code': 0}
    else:
        raise ValueError('Only run, read, and write use the sandbox bridge')
except Exception as exc:
    result = {'stdout': '', 'stderr': '%s: %s' % (type(exc).__name__, exc), 'exit_code': 1}
print(json.dumps(result, ensure_ascii=False))
'''


def _utc() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _call(args: list[str], *, timeout: int = 30, data: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, input=data, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, shell=False)


class RoleSandbox:
    """Owns only its recorded container IDs, with strict mount-based role access."""

    def __init__(self, task_dir: Path, run_dir: Path, image: str = DEFAULT_IMAGE):
        self.task_dir = Path(task_dir).resolve()
        self.run_dir = Path(run_dir).resolve()
        self.image = image
        self.prefix = "dpswarm-tb-" + uuid.uuid4().hex[:12]
        self.containers: dict[str, str] = {}
        self.workspace = self.run_dir / "workspace"
        self.reports = self.run_dir / "reports"
        self.submission = self.run_dir / "submission"
        self.grader = self.run_dir / "grader"
        self.logs = self.run_dir / "sandbox"
        self._closed = False
        self._freeze_started = False
        self._frozen = False
        self._frozen_roles: list[str] = []

    def _base_args(self, role: str) -> list[str]:
        return ["docker", "run", "--detach", "--name", self.prefix + "-" + role,
                "--label", "dpswarm.teambench.owner=" + self.prefix,
                "--init", "--user", "10001:10001", "--network", "none",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges=true",
                "--read-only", "--memory", "1g", "--memory-swap", "1g", "--cpus", "2",
                "--pids-limit", "128", "--ulimit", "nofile=1024:1024",
                "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=268435456,mode=1777"]

    @staticmethod
    def _mount(source: Path, target: str, readonly: bool) -> list[str]:
        source = source.resolve(strict=True)
        # Docker's --mount CSV grammar cannot safely express arbitrary commas.
        if "," in str(source):
            raise ValueError("Docker mount source paths may not contain commas")
        return ["--mount", f"type=bind,source={source},target={target}" + (",readonly" if readonly else "")]

    def _save_manifest(self) -> None:
        self.logs.mkdir(parents=True, exist_ok=True)
        value = {"image": self.image, "prefix": self.prefix, "containers": self.containers,
                 "task_dir": str(self.task_dir), "run_dir": str(self.run_dir),
                 "updated_at": _utc(), "closed": self._closed,
                 "freeze_started": self._freeze_started, "frozen": self._frozen,
                 "frozen_roles": self._frozen_roles}
        (self.logs / "containers.json").write_text(json.dumps(value, indent=2), encoding="utf-8")

    def start(self) -> "RoleSandbox":
        if self.containers or self._closed:
            raise RuntimeError("Sandbox instances can only be started once")
        for name in ("spec.md", "brief.md", "grade.sh"):
            source = self.task_dir / name
            if not source.is_file() or source.resolve().parent != self.task_dir:
                raise ValueError(f"Required regular task file missing or escaped task directory: {name}")
        if not self.workspace.exists() and (self.task_dir / "workspace").is_dir():
            shutil.copytree(self.task_dir / "workspace", self.workspace)
        if not self.workspace.is_dir() or not any(self.workspace.iterdir()):
            raise ValueError("Prepare a nonempty seed-specific run/workspace before start()")
        for directory in (self.workspace, self.reports, self.submission, self.grader, self.logs):
            directory.mkdir(parents=True, exist_ok=True)
            if directory.resolve().parent != self.run_dir:
                raise ValueError(f"Run directory is a symlink or escapes run root: {directory}")
        if (self.reports / "expected.json").exists():
            raise ValueError("Ground truth expected.json must be in run/grader, never role reports")
        inspect = _call(["docker", "image", "inspect", self.image])
        if inspect.returncode:
            raise RuntimeError("Sandbox image unavailable: " + inspect.stderr.strip())
        (self.logs / "image.inspect.json").write_text(inspect.stdout, encoding="utf-8")
        try:
            for role in ROLES:
                command = self._base_args(role)
                command += self._mount(self.task_dir / "brief.md", "/task/brief.md", True)
                if role != "executor":
                    command += self._mount(self.task_dir / "spec.md", "/task/spec.md", True)
                if role != "planner":
                    command += self._mount(self.workspace, "/shared/workspace", role == "verifier")
                    command += self._mount(self.reports, "/shared/reports", role == "verifier")
                if role in ("verifier", "oracle"):
                    command += self._mount(self.submission, "/shared/submission", False)
                command += ["--workdir", "/task" if role == "planner" else "/shared/workspace",
                            self.image, "sleep", "infinity"]
                result = _call(command, timeout=60)
                if result.returncode:
                    raise RuntimeError(f"Failed to start {role}: {result.stderr.strip()}")
                self.containers[role] = result.stdout.strip()
                self._save_manifest()
            details = _call(["docker", "inspect", *self.containers.values()])
            if details.returncode:
                raise RuntimeError("Failed to inspect role containers: " + details.stderr)
            (self.logs / "roles.inspect.json").write_text(details.stdout, encoding="utf-8")
            return self
        except BaseException:
            self.close()
            raise

    def tool(self, role: str, name: str, args: dict) -> dict:
        if role not in ROLES or role not in self.containers or self._closed:
            return {"stdout": "", "stderr": "Role sandbox is not running", "exit_code": 1}
        if self._freeze_started:
            return {"stdout": "", "stderr": "Role sandbox is frozen; candidate tools are disabled", "exit_code": 1}
        if name not in ("run", "read", "write") or not isinstance(args, dict):
            return {"stdout": "", "stderr": "Unsupported tool or invalid arguments", "exit_code": 1}
        started = time.monotonic()
        try:
            result = _call(["docker", "exec", "-i", self.containers[role], "python", "-c",
                            _TOOL_SCRIPT, role, name], timeout=TOOL_TIMEOUT + 15,
                           data=json.dumps(args, ensure_ascii=False))
            if result.returncode:
                value = {"stdout": result.stdout[:OUTPUT_LIMIT], "stderr": result.stderr[:OUTPUT_LIMIT],
                         "exit_code": result.returncode}
            else:
                value = json.loads(result.stdout)
                if not isinstance(value, dict) or not all(k in value for k in ("stdout", "stderr", "exit_code")):
                    raise ValueError("Invalid tool bridge response")
        except subprocess.TimeoutExpired:
            # If the in-container timeout itself failed, terminate the owned role
            # container so a detached command cannot continue mutating this run.
            _call(["docker", "kill", self.containers[role]])
            value = {"stdout": "", "stderr": "Container tool exceeded 75s; role container stopped", "exit_code": 124}
        except (OSError, ValueError) as exc:
            value = {"stdout": "", "stderr": str(exc), "exit_code": 1}
        with (self.logs / "tools.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": _utc(), "role": role, "tool": name, "args": args,
                                     "result": value, "wall_seconds": time.monotonic()-started},
                                    ensure_ascii=False) + "\n")
        return value

    def freeze(self) -> list[str]:
        """Idempotently stop candidate execution before reading final artifacts.

        Paused containers retain their files but cannot mutate them. Once freeze
        has been requested, candidate tools stay disabled even if Docker reports
        a partial failure; callers may retry freeze() or close the sandbox.
        """
        if self._closed or not self.containers:
            raise RuntimeError("Start the sandbox before freezing")
        if self._frozen:
            return list(self._frozen_roles)
        self._freeze_started = True
        self._save_manifest()
        for role, container_id in self.containers.items():
            if role not in ROLES:
                continue
            state = _call(["docker", "inspect", "--format", "{{.State.Running}} {{.State.Paused}}", container_id])
            if state.returncode:
                raise RuntimeError("Cannot inspect role before freezing: " + state.stderr)
            status = state.stdout.strip()
            if status == "true false":
                paused = _call(["docker", "pause", container_id])
                if paused.returncode:
                    raise RuntimeError("Cannot freeze role: " + paused.stderr)
            if status in ("true false", "true true") and role not in self._frozen_roles:
                self._frozen_roles.append(role)
        self._frozen = True
        self._save_manifest()
        return list(self._frozen_roles)

    def grade(self) -> dict:
        """Run the untouched task grader in a separate, network-free container.

        Grading has a 180 second wall limit. Raw score is accepted only when
        newly written by this grader; any previous candidate score is moved aside.
        """
        if self._closed or not self.containers:
            raise RuntimeError("Start the sandbox before grading")
        if "grader" in self.containers:
            raise RuntimeError("This sandbox has already been graded")
        grade_log = self.logs / "grade"
        grade_log.mkdir(parents=True, exist_ok=True)
        # Freeze candidate processes before the final score. Otherwise a command
        # that daemonized could race the grader or overwrite score.json later.
        frozen = self.freeze()
        (grade_log / "frozen_roles.json").write_text(json.dumps(frozen), encoding="utf-8")
        score_path = self.reports / "score.json"
        if score_path.is_symlink() or not score_path.resolve().is_relative_to(self.reports.resolve()):
            raise ValueError("Refusing symlinked score.json")
        if score_path.exists():
            score_path.replace(grade_log / "candidate_score.ignored.json")
        command = self._base_args("grader")
        command += self._mount(self.task_dir, "/task", True)
        command += self._mount(self.workspace, "/shared/workspace", False)
        command += self._mount(self.reports, "/shared/reports", False)
        command += self._mount(self.submission, "/shared/submission", True)
        command += self._mount(self.grader, "/grader", True)
        command += ["--workdir", "/shared/workspace", self.image, "sleep", "infinity"]
        created = _call(command, timeout=60)
        if created.returncode:
            raise RuntimeError("Failed to start grader: " + created.stderr)
        self.containers["grader"] = created.stdout.strip()
        self._save_manifest()
        started = time.monotonic()
        timed_out = False
        # Files preserve complete diagnostic output without holding it in memory.
        with (grade_log / "stdout.log").open("wb") as stdout, (grade_log / "stderr.log").open("wb") as stderr:
            try:
                process = subprocess.run(["docker", "exec", self.containers["grader"],
                    "timeout", "--signal=TERM", "--kill-after=5s", str(GRADE_TIMEOUT),
                    "bash", "/task/grade.sh", "/workspace", "/reports", "/submission", "/task", "/grader/expected.json"],
                    stdout=stdout, stderr=stderr, timeout=GRADE_TIMEOUT, shell=False)
                exit_code = process.returncode
                timed_out = exit_code in (124, 137)
            except subprocess.TimeoutExpired:
                timed_out, exit_code = True, 124
            finally:
                _call(["docker", "kill", self.containers["grader"]])
        raw = None
        parse_error = None
        if score_path.is_file() and not score_path.is_symlink():
            payload = score_path.read_bytes()
            (grade_log / "score.raw.json").write_bytes(payload)
            try:
                raw = json.loads(payload)
            except (ValueError, UnicodeDecodeError) as exc:
                parse_error = str(exc)
        value = {"exit_code": exit_code, "timed_out": timed_out, "raw_score": raw,
                 "score": raw, "score_parse_error": parse_error,
                 "wall_seconds": time.monotonic()-started, "stdout_path": str(grade_log / "stdout.log"),
                 "stderr_path": str(grade_log / "stderr.log"), "raw_score_path": str(grade_log / "score.raw.json"),
                 "grade_sha256": hashlib.sha256((self.task_dir / "grade.sh").read_bytes()).hexdigest()}
        (grade_log / "result.json").write_text(json.dumps(value, indent=2), encoding="utf-8")
        return value

    def close(self) -> None:
        """Remove only IDs created and recorded by this exact sandbox instance."""
        errors = []
        for role, container_id in self.containers.items():
            try:
                result = _call(["docker", "rm", "--force", container_id], timeout=30)
                if result.returncode and "No such container" not in result.stderr:
                    errors.append({"role": role, "id": container_id, "stderr": result.stderr})
            except (OSError, subprocess.TimeoutExpired) as exc:
                errors.append({"role": role, "id": container_id, "stderr": str(exc)})
        self._closed = True
        self._save_manifest()
        (self.logs / "cleanup.json").write_text(json.dumps({"at": _utc(), "errors": errors}, indent=2), encoding="utf-8")
        if errors:
            raise RuntimeError("Could not remove some owned containers; see sandbox/cleanup.json")

    def __enter__(self) -> "RoleSandbox":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.close()


def build_image(directory: Path, image: str = DEFAULT_IMAGE) -> dict:
    """Build only the Dockerfile (stdin context excludes all workspace secrets)."""
    directory = Path(directory).resolve()
    log = directory / "sandbox_build.log"
    with (directory / "Dockerfile").open("rb") as source, log.open("wb") as output:
        proc = subprocess.run(["docker", "build", "--progress=plain", "--tag", image, "-"],
                              stdin=source, stdout=output, stderr=subprocess.STDOUT, timeout=900, shell=False)
    result = {"exit_code": proc.returncode, "image": image, "log": str(log)}
    if proc.returncode:
        raise RuntimeError("Image build failed; see " + str(log))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    args = parser.parse_args()
    if args.build:
        print(json.dumps(build_image(Path(__file__).parent, args.image), indent=2))
    else:
        parser.error("Use --build, or import RoleSandbox from the experiment driver")


if __name__ == "__main__":
    main()
