"""Reviewable A2 preparation, kept outside the already frozen A1 runtime.

stage_a2_inputs is local-only. Publication and qualification are separate,
explicit operations which require a successful A1 and its released resource
lease. No function calls a candidate model. Qualification may use Docker only
when explicitly invoked by the authorized outer pipeline.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import uuid

from .. import data

OPERATIONS = Path(__file__).resolve().parent
OFFICIAL = OPERATIONS.parent / "official"
STAGING = OPERATIONS / "staging_a2"
PROTOCOL = "minimum_value_a2_preparation_v1"
CHECK_FILES = ("public_checks.json", "public_checks_provenance.json")


class PreparationStopped(RuntimeError):
    pass


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _sha(path):
    return data.sha256(Path(path))


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _official():
    # This operation never accepts an arbitrary historical official directory.
    return OFFICIAL.resolve()


def _staging(path=None):
    path = Path(path or STAGING).resolve()
    if not path.is_relative_to(OPERATIONS.resolve()):
        raise ValueError("A2 staging must remain inside this experiment's operations directory")
    return path


def _validate_definitions(definitions, provenance, public):
    expected = {row["instance_id"]: row for row in public}
    if set(definitions) != set(expected) or set(provenance) != set(expected) or len(expected) != 16:
        raise ValueError("A2 definitions must cover exactly the frozen 16 D1 instances")
    for instance_id, row in expected.items():
        checks, evidence = definitions[instance_id], provenance[instance_id]
        if not isinstance(checks, dict) or not checks or len(checks) > 4:
            raise ValueError("Each D1 task needs one to four bounded public checks")
        if (evidence.get("repo") != row["repo"] or evidence.get("base_commit") != row["base_commit"]
                or evidence.get("problem_statement_sha256") != hashlib.sha256(row["problem_statement"].encode()).hexdigest()
                or set(evidence.get("checks", {})) != set(checks)):
            raise ValueError("Public-check evidence is not bound to the frozen public task")
        for check_id, command in checks.items():
            if (not isinstance(check_id, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", check_id)
                    or not isinstance(command, str) or not command.strip() or len(command) > 4000):
                raise ValueError("Invalid frozen public-check definition")
            check = evidence["checks"][check_id]
            if check.get("command") != command or check.get("timeout_seconds") != 120 or not check.get("rationale"):
                raise ValueError("Public-check command, timeout or rationale is not frozen")
            sources = check.get("sources") or []
            if not sources:
                raise ValueError("Public checks require a verified base-commit source file")
            for source in sources:
                relative = source.get("path", "")
                if (not relative or relative.startswith(("/", "\\")) or ".." in Path(relative).parts
                        or not re.fullmatch(r"[0-9a-f]{64}", source.get("sha256", ""))
                        or source.get("url") != f"https://raw.githubusercontent.com/{row['repo']}/{row['base_commit']}/{relative}"):
                    raise ValueError("Public-check source lacks exact commit/path/hash provenance")


def _verify_staged(stage):
    manifest_path = stage / "manifest.json"
    manifest = _read(manifest_path)
    if manifest.get("protocol") != PROTOCOL or manifest.get("task_count") != 16:
        raise ValueError("Incompatible A2 staging manifest")
    if (set(manifest.get("outputs", {})) != set(CHECK_FILES)
            or set(manifest.get("public_inputs", {})) != {"selection.json", "selected_public.json"}
            or set(manifest.get("definition_sources", {})) != {"definitions", "provenance"}):
        raise ValueError("Incomplete A2 staging bindings")
    for name, expected in manifest["outputs"].items():
        if name not in CHECK_FILES or _sha(stage / name) != expected:
            raise ValueError("Frozen staged public checks changed")
    for name, expected in manifest["public_inputs"].items():
        if name not in {"selection.json", "selected_public.json"} or _sha(_official() / name) != expected:
            raise ValueError("D1 selection changed after staging")
    for label, source in manifest["definition_sources"].items():
        if _sha(source["path"]) != source["sha256"]:
            raise ValueError("Reviewed A2 public-check definitions changed: " + label)
    return manifest


def stage_a2_inputs(*, definitions_path=None, provenance_path=None, staging_dir=None):
    """Validate and merge public-only definitions; write solely under operations."""
    stage = _staging(staging_dir)
    if (stage / "manifest.json").exists():
        _verify_staged(stage)
        return {"staged": True, "manifest_path": str(stage / "manifest.json"),
                "manifest_sha256": _sha(stage / "manifest.json"), "task_count": 16}
    definitions_path = Path(definitions_path or OPERATIONS / "public_check_definitions.json").resolve()
    provenance_path = Path(provenance_path or OPERATIONS / "public_check_provenance.json").resolve()
    definitions, provenance = _read(definitions_path), _read(provenance_path)
    official = _official()
    public = data.load_public("d1", official)
    _validate_definitions(definitions, provenance, public)
    a1_ids = {row["instance_id"] for row in data.load_public("a1", official)}
    current_checks = _read(official / "public_checks.json")
    current_provenance = _read(official / "public_checks_provenance.json")
    if not a1_ids.issubset(current_checks) or not a1_ids.issubset(current_provenance):
        raise ValueError("Frozen A1 public checks are missing")
    merged_checks = {**{key: current_checks[key] for key in sorted(a1_ids)}, **definitions}
    merged_provenance = {**{key: current_provenance[key] for key in sorted(a1_ids)}, **provenance}
    data.freeze_json(stage / CHECK_FILES[0], merged_checks)
    data.freeze_json(stage / CHECK_FILES[1], merged_provenance)
    manifest = {"protocol": PROTOCOL, "created_at": _utc(), "task_count": 16,
        "a1_ids": sorted(a1_ids), "d1_ids": [row["instance_id"] for row in public],
        "public_inputs": {name: _sha(official / name) for name in ("selection.json", "selected_public.json")},
        "definition_sources": {"definitions": {"path": str(definitions_path), "sha256": _sha(definitions_path)},
                               "provenance": {"path": str(provenance_path), "sha256": _sha(provenance_path)}},
        "outputs": {name: _sha(stage / name) for name in CHECK_FILES}}
    data.freeze_json(stage / "manifest.json", manifest)
    return {"staged": True, "manifest_path": str(stage / "manifest.json"),
            "manifest_sha256": _sha(stage / "manifest.json"), "task_count": 16}


prepare_a2_inputs = stage_a2_inputs


def _approved_a1(a1_batch):
    from .. import cli, freeze
    from ..transport_identity import transport_identity
    a1_batch = Path(a1_batch).resolve()
    manifest = cli.load_manifest(a1_batch)
    state = _read(a1_batch / "state.json")
    if not state.get("completed_at") or state.get("active_run_id"):
        raise ValueError("A1 must finish before publishing or qualifying A2 inputs")
    evidence = cli.validate_a1(a1_batch, expected_sources=freeze.runtime_sources(),
                              expected_transport=transport_identity())
    snapshot = freeze.verify_snapshot(a1_batch, manifest).resolve()
    snapshot_official = snapshot / "modelbench" / "minimal_value_20260905" / "official"
    if snapshot_official.resolve() == _official():
        raise ValueError("A1 must use its independent runtime/input snapshot")
    for name in CHECK_FILES:
        if os.path.samefile(snapshot_official / name, _official() / name):
            raise ValueError("A1 public-check files must be independent copies")
    lock = manifest.get("resource_lock_path")
    if not lock or not Path(lock).is_absolute():
        raise ValueError("A1 does not bind the shared resource lease")
    return evidence, manifest, snapshot


def _atomic_bytes(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".pending")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _publication_record(stage):
    path = stage / "publication" / "publication.json"
    record = _read(path)
    if (record.get("published") is not True or record.get("stage_manifest_sha256") != _sha(stage / "manifest.json")
            or set(record.get("input_artifacts", {})) != set(CHECK_FILES)):
        raise ValueError("A2 publication is absent or does not match staged inputs")
    for name, expected in record["input_artifacts"].items():
        if name not in CHECK_FILES or _sha(_official() / name) != expected:
            raise ValueError("Published A2 inputs changed or publication is incomplete")
    return {**record, "publication_path": str(path), "publication_sha256": _sha(path)}


def publish_a2_inputs(a1_batch, *, staging_dir=None):
    """After A1, back up both old files and replace only this experiment's inputs.

    The two file replacements share a persistent intent. A crash between them
    leaves no committed publication; a repeat accepts only the recorded old/new
    hashes and finishes it. Qualification requires the commit record and both
    new hashes. The A1 snapshot is verified again after publication.
    """
    from .. import cli, freeze
    stage = _staging(staging_dir)
    staged = _verify_staged(stage)
    evidence, manifest, _ = _approved_a1(a1_batch)
    record_dir = stage / "publication"
    with cli.stage_lease(manifest["resource_lock_path"], record_dir):
        if (record_dir / "publication.json").exists():
            return _publication_record(stage)
        intent_path = record_dir / "intent.json"
        if intent_path.exists():
            intent = _read(intent_path)
            if intent["stage_manifest_sha256"] != _sha(stage / "manifest.json") or intent["a1_evidence"] != evidence:
                raise ValueError("An interrupted publication belongs to different inputs or A1")
        else:
            old = {}
            for name in CHECK_FILES:
                content = (_official() / name).read_bytes()
                backup = record_dir / "a1_backups" / name
                if backup.exists() and backup.read_bytes() != content:
                    raise ValueError("A1 input backup differs from the current pre-publication input")
                if not backup.exists():
                    _atomic_bytes(backup, content)
                old[name] = hashlib.sha256(content).hexdigest()
            intent = {"protocol": PROTOCOL, "a1_evidence": evidence,
                "stage_manifest_sha256": _sha(stage / "manifest.json"),
                "old_artifacts": old, "new_artifacts": staged["outputs"]}
            data.freeze_json(intent_path, intent)
        for name in CHECK_FILES:
            if _sha(record_dir / "a1_backups" / name) != intent["old_artifacts"][name]:
                raise ValueError("A1 public-check backup changed")
            if _sha(_official() / name) not in {intent["old_artifacts"][name], intent["new_artifacts"][name]}:
                raise ValueError("Official public checks changed outside the recorded publication")
        for name in CHECK_FILES:
            _atomic_bytes(_official() / name, (stage / name).read_bytes())
        freeze.verify_snapshot(Path(a1_batch), manifest)
        data.freeze_json(record_dir / "publication.json", {"protocol": PROTOCOL, "published": True,
            "published_at": _utc(), "a1_evidence": evidence, "stage_manifest_sha256": _sha(stage / "manifest.json"),
            "backup_artifacts": intent["old_artifacts"], "input_artifacts": intent["new_artifacts"]})
        return _publication_record(stage)


def qualification_owned_root(attempt="a2_attempt_01"):
    if not isinstance(attempt, str) or not re.fullmatch(r"a2_attempt_[0-9]{2,}", attempt):
        raise ValueError("Invalid A2 qualification attempt")
    return _official() / "grader" / "preflight" / "a2_runs" / attempt


def _admit(deadline, stop_file):
    if stop_file and Path(stop_file).exists():
        raise PreparationStopped("stop requested")
    if time.monotonic() >= deadline:
        raise PreparationStopped("preparation admission deadline reached")


def _official_counts(instance_id, job, directory, contract):
    """Same official parser as A1; parser container has its own cleanup intent."""
    from .. import environment, cli
    owner = uuid.uuid4().hex
    name = "dpswarm-a2-parser-" + owner[:16]
    parser_dir = directory / "counts"
    data.freeze_json(parser_dir / "request.json", {"owner": owner, "instance_id": instance_id, "grader_contract": contract})
    data.freeze_json(parser_dir / "controller.json", [name])
    try:
        result = environment._docker(["create", "--name", name, "--label", "dpswarm.swe.owner=" + owner,
            "--network", "none", "--memory", "768m", "--cpus", "1", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--mount", f"type=bind,source={Path(job).resolve()},target=/job,readonly",
            "--mount", f"type=bind,source={_official() / 'grader/test_specs.json'},target=/specs.json,readonly",
            _read(_official() / "grader_controller.json")["image_id"],
            "-c", data._COUNTS_SCRIPT, instance_id])
        # Keep the name intent immutable; exact owner/name remains resolvable.
        result = environment._docker(["start", "--attach", result.stdout.strip()], timeout=120)
        return json.loads(result.stdout)
    finally:
        cleanup = cli.cleanup_owned_episode(parser_dir)
        if cleanup.get("confirmed") is not True:
            raise RuntimeError("official parser cleanup was not confirmed")



def public_check_execution(command, result):
    """Count actually executed public tests; no gold or private parser is used."""
    output = str(result.get("stdout", "")) + "\n" + str(result.get("stderr", ""))
    output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
    evidence = {"framework": None, "executed": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    if result.get("timed_out") or result.get("exit_code") not in (0, 1):
        return evidence
    if re.search(r"\bpython\s+-m\s+pytest\b", command):
        evidence["framework"] = "pytest"
        summaries = [line for line in output.splitlines()
                     if re.search(r"\bin\s+[0-9.]+\s*(?:s|seconds?)\b", line)
                     and re.search(r"\b[0-9]+\s+(?:passed|failed|skipped|xfailed|xpassed|errors?)\b", line)]
        if not summaries:
            return evidence
        counts = {name.rstrip("s") if name in {"errors", "error"} else name: int(number)
                  for number, name in re.findall(r"\b([0-9]+)\s+(passed|failed|skipped|xfailed|xpassed|errors?)\b", summaries[-1])}
        evidence.update(passed=counts.get("passed", 0), failed=counts.get("failed", 0),
                        errors=counts.get("error", 0), skipped=counts.get("skipped", 0))
    elif re.search(r"\bpython\s+(?:-m\s+unittest\b|tests/runtests\.py\b)", command):
        evidence["framework"] = "unittest"
        runs = list(re.finditer(r"\bRan\s+([0-9]+)\s+tests?\s+in\s+", output))
        if not runs:
            return evidence
        tail = output[runs[-1].end():]
        if not re.search(r"(?m)^(?:OK|FAILED)\b", tail):
            return evidence
        total = int(runs[-1].group(1))
        for source, target in (("failures", "failed"), ("errors", "errors"), ("skipped", "skipped")):
            matches = re.findall(source + r"=([0-9]+)", tail)
            evidence[target] = int(matches[-1]) if matches else 0
        evidence["passed"] = max(0, total - evidence["failed"] - evidence["errors"] - evidence["skipped"])
    if evidence["errors"] == 0:
        evidence["executed"] = evidence["passed"] + evidence["failed"]
    return evidence


def _project_qualification(instance_id, attempt, summary):
    # Frozen CLI orders only attempt_N directory names. Keep a2_attempt_N for
    # unique owned resource roots, and project every result into attempt_N.
    match = re.fullmatch(r"a2_attempt_([0-9]{2,})", attempt)
    if not match:
        raise ValueError("Invalid A2 qualification attempt")
    gate_attempt = "attempt_" + match.group(1)
    target = _official() / "grader" / "preflight" / instance_id / gate_attempt / "qualification.json"
    data.freeze_json(target, summary)
    return target


def _qualify_one(instance, *, contract, checks, owned_root, attempt, deadline, stop_file):
    from .. import environment, cli
    instance_id = instance["instance_id"]
    directory = owned_root / instance_id
    summary_path = directory / "qualification.json"
    if summary_path.exists():
        summary = _read(summary_path)
        if summary.get("grader_contract") != contract or summary.get("instance_id") != instance_id:
            raise ValueError("Cached A2 qualification has a different frozen contract")
        if summary.get("qualified") and not data.qualification_passes(summary.get("baseline", {}), summary.get("reference", {})):
            raise ValueError("Cached A2 qualification is not supported by official counts")
        _project_qualification(instance_id, attempt, summary)
        return summary
    _admit(deadline, stop_file)
    started = time.monotonic()
    summary = {"instance_id": instance_id, "qualified": False, "grader_contract": contract,
        "preparation_cost_category": "no-model environment and grader preparation", "attempt": attempt}
    try:
        info = environment.ensure_image(instance_id)  # one sequential, bounded pull
        summary["image_id"] = info["image_id"]
        _admit(deadline, stop_file)
        env = environment.ValueEnvironment(instance, directory / "candidate_probe", image=info["image_id"], grader_contract=contract)
        try:
            env.start()
            commands = {}
            for check_id, command in checks.items():
                _admit(deadline, stop_file)
                result = env.run(command, timeout=min(120, max(1, int(deadline - time.monotonic()))))
                executed = public_check_execution(command, result)
                commands[check_id] = {**{key: result[key] for key in ("exit_code", "timed_out", "duration_seconds")},
                                      "observed_execution": executed}
                if executed["executed"] < 1:
                    raise RuntimeError("public check did not execute real tests within its frozen scope")
            env.run("(sleep 20; printf late > /tmp/minimum-value-late-writer) >/tmp/minimum-value-writer.log 2>&1 < /dev/null &")
            quiesce = env.quiesce()
            time.sleep(21)
            absent = environment._docker(["exec", env.container_id, "/usr/bin/test", "!", "-e", "/tmp/minimum-value-late-writer"], check=False)
            applicability = env.check_frozen_patch(env.export_patch())
            if absent.returncode or not applicability["applicable"]:
                raise RuntimeError("candidate lifecycle probe failed")
            summary["candidate_probe"] = {"public_checks": commands, "quiesced": quiesce["quiesced"],
                "detached_writer_stopped": True, "patch_applicable": True}
        finally:
            closed = env.close()
        if not closed["closed"]:
            raise RuntimeError("candidate cleanup not confirmed")
        # Private contents remain in host-only grader files and never in returned summaries.
        private = next(row for row in _read(_official() / "grader/selected.json") if row["instance_id"] == instance_id)
        for kind in ("baseline", "reference"):
            _admit(deadline, stop_file)
            patch = ("diff --git a/.dpswarm_baseline_qualification b/.dpswarm_baseline_qualification\n"
                "new file mode 100644\n--- /dev/null\n+++ b/.dpswarm_baseline_qualification\n@@ -0,0 +1 @@\n+baseline qualification marker\n"
                if kind == "baseline" else private["patch"])
            content = patch.encode("utf-8")
            patch_path = directory / kind / "frozen.patch"
            if patch_path.exists() and patch_path.read_bytes() != content:
                raise ValueError("Private qualification patch changed")
            if not patch_path.exists():
                _atomic_bytes(patch_path, content)
            phase_started = time.monotonic()
            grade = environment.rootgrade_terminal(instance, patch_path, hashlib.sha256(content).hexdigest(),
                directory / kind / "evaluation", image=info["image_id"], grader_contract=contract,
                model_name="a2_preflight_" + kind, timeout=min(900, max(1, int(deadline - time.monotonic()))))
            counts = _official_counts(instance_id, grade["grader_dir"], directory / kind, contract)
            summary[kind] = {"completed": grade.get("completed") is True, "resolved": grade.get("resolved"),
                "counts": counts, "duration_seconds": time.monotonic() - phase_started}
        summary["qualified"] = data.qualification_passes(summary["baseline"], summary["reference"])
    except Exception as exc:
        data.freeze_json(directory / "private_error.json", {"error_type": type(exc).__name__, "error": str(exc)})
        summary["error_type"] = type(exc).__name__
    finally:
        cleanup = cli.cleanup_owned_episode(directory)
        summary["cleanup_confirmed"] = cleanup.get("confirmed") is True
        if not summary["cleanup_confirmed"]:
            summary["qualified"] = False
            summary["error_type"] = "CleanupUnconfirmed"
    summary["duration_seconds"] = time.monotonic() - started
    data.freeze_json(summary_path, summary)
    # The existing A2 gate searches under each task ID; only the public count
    # summary is mirrored, while all owned resources remain under owned_root.
    _project_qualification(instance_id, attempt, summary)
    return summary


def qualify_a2(a1_batch, *, attempt="a2_attempt_01", stop_file=None, wall_seconds=57600, staging_dir=None):
    """At most 16 tasks, serially; stop on the first failed qualification.

    wall_seconds closes admission and reduces grading timeouts. Legacy image
    pulls/setup retain their own finite timeouts; the outer pipeline must own
    the process watchdog and clean only qualification_owned_root(attempt).
    """
    from .. import environment, cli
    if type(wall_seconds) not in (int, float) or not math.isfinite(wall_seconds) or not 1 <= wall_seconds <= 57600:
        raise ValueError("A2 qualification wall_seconds must be between 1 and 57600")
    stage = _staging(staging_dir)
    _verify_staged(stage)
    publication = _publication_record(stage)
    evidence, manifest, _ = _approved_a1(a1_batch)
    if publication["a1_evidence"] != evidence:
        raise ValueError("A2 inputs were published for a different A1 predecessor")
    owned_root = qualification_owned_root(attempt)
    started = time.monotonic()
    result = {"all_qualified": False, "qualified_count": 0, "total": 16, "stopped": False,
              "failure_kind": None, "qualification_paths": [], "owned_root": str(owned_root)}
    with cli.stage_lease(manifest["resource_lock_path"], owned_root) as lease:
        contract = environment.capture_grader_contract()
        data.freeze_json(owned_root / "preparation-contract.json", {"protocol": PROTOCOL,
            "source_sha256": _sha(Path(__file__)), "grader_contract": contract,
            "publication_sha256": publication["publication_sha256"], "a1_evidence": evidence,
            "attempt": attempt, "wall_seconds": wall_seconds})
        if (owned_root / "operation-summary.json").exists():
            return _read(owned_root / "operation-summary.json")
        public, checks = data.load_public("d1", _official()), _read(_official() / "public_checks.json")
        if len(public) != 16:
            raise ValueError("D1 must retain all 16 frozen instances")
        for instance in public:
            try:
                _admit(started + wall_seconds, stop_file)
            except PreparationStopped:
                result.update(stopped=True, failure_kind="preparation_stopped")
                break
            if environment.capture_grader_contract() != contract:
                raise ValueError("Grader inputs changed between D1 qualification tasks")
            summary = _qualify_one(instance, contract=contract, checks=checks[instance["instance_id"]],
                owned_root=owned_root, attempt=attempt, deadline=started + wall_seconds, stop_file=stop_file)
            result["qualification_paths"].append(str(owned_root / instance["instance_id"] / "qualification.json"))
            if summary.get("cleanup_confirmed") is not True:
                lease["retain"] = True
            if not summary["qualified"]:
                result.update(stopped=True, failure_kind=summary.get("error_type") or "qualification_failed")
                break
            result["qualified_count"] += 1
        result["all_qualified"] = result["qualified_count"] == 16
        result["elapsed_seconds"] = time.monotonic() - started
        data.freeze_json(owned_root / "operation-summary.json", result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("stage", "publish", "qualify"))
    parser.add_argument("--a1-batch", type=Path)
    parser.add_argument("--staging-dir", type=Path)
    parser.add_argument("--attempt", default="a2_attempt_01")
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--wall-seconds", type=float, default=57600)
    args = parser.parse_args()
    if args.operation == "stage":
        result = stage_a2_inputs(staging_dir=args.staging_dir)
    elif not args.a1_batch:
        parser.error("--a1-batch is required")
    elif args.operation == "publish":
        result = publish_a2_inputs(args.a1_batch, staging_dir=args.staging_dir)
    else:
        result = qualify_a2(args.a1_batch, attempt=args.attempt, stop_file=args.stop_file,
                            wall_seconds=args.wall_seconds, staging_dir=args.staging_dir)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if args.operation == "qualify" and not result["all_qualified"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
