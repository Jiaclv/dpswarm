"""Independent, metadata-only selection and private official grader preparation."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.request

from .contracts import A1_TASKS, PUBLIC_FIELDS, SEED
from modelbench.swe_verified_20260903.prepare_dataset import (
    DATASET, DATASET_REVISION, DATASET_SHA256, HARNESS_REVISION,
)

ROOT = Path(__file__).resolve().parent
OFFICIAL = ROOT / "official"
OLD_OFFICIAL = ROOT.parent / "swe_verified_20260903" / "official"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze_json(path: Path, value) -> None:
    """Idempotent write; an existing preparation artifact is never replaced."""
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != value:
            raise ValueError(f"existing frozen artifact differs: {path.name}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def select_metadata(rows, exposed=(), *, count=16, seed=SEED, min_repos=6, max_per_repo=3):
    """Round-robin hash-ordered repository strata; only IDs and repos are read."""
    if count < 1 or min_repos < 1 or max_per_repo < 1 or count < min_repos:
        raise ValueError("invalid selection constraints")
    metadata = [{"instance_id": row["instance_id"], "repo": row["repo"]} for row in rows]
    if len({row["instance_id"] for row in metadata}) != len(metadata):
        raise ValueError("duplicate instance IDs")
    excluded = set(exposed) | set(A1_TASKS)
    metadata = [row for row in metadata if row["instance_id"] not in excluded]
    digest = lambda value: hashlib.sha256(f"{seed}:{value}".encode()).hexdigest()
    repos = sorted({row["repo"] for row in metadata}, key=lambda repo: (digest(f"repo:{repo}"), repo))
    if len(repos) < min_repos:
        raise ValueError("too few unexposed repository strata")
    groups = {repo: sorted([row for row in metadata if row["repo"] == repo],
                          key=lambda row: (digest(f"instance:{row['instance_id']}"), row["instance_id"]))
              for repo in repos}
    selected = []
    for position in range(max_per_repo):
        for repo in repos:
            if len(groups[repo]) > position:
                selected.append(groups[repo][position])
                if len(selected) == count:
                    if len({row["repo"] for row in selected}) < min_repos:
                        raise ValueError("selection violates minimum repository count")
                    return selected
    raise ValueError("too few unexposed instances under repository cap")


def exposure_inventory(modelbench: Path | None = None):
    """Read historical public selections/manifests, plus task IDs in path names.

    Gold data, model traces and credentials are not read. Every historical
    selected task is excluded conservatively, even if it was never dispatched.
    """
    modelbench = modelbench or ROOT.parent
    sources, ids = [], set()
    pattern = re.compile(r"[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[0-9]+")

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "instance_id" and isinstance(item, str) and pattern.fullmatch(item):
                    ids.add(item)
                elif isinstance(item, (dict, list)):
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for name in ("swe_verified_20260903", "swe_fixed_team_20260903"):
        directory = modelbench / name
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            relative = path.relative_to(modelbench)
            if any(part in {".git", "SWE-bench", "source_snapshot", "source_snapshots"} for part in relative.parts):
                continue
            ids.update(pattern.findall(str(relative)))
            if path.is_file() and path.name in {"manifest.json", "selected_public.json", "selection.json"}:
                visit(json.loads(path.read_text(encoding="utf-8")))
                sources.append({"path": relative.as_posix(), "sha256": sha256(path)})
    return {"policy": "exclude all historical public selections, manifests, and task-named artifacts",
            "instance_ids": sorted(ids), "sources": sorted(sources, key=lambda row: row["path"])}


def load_public(pool="a1", official: Path = OFFICIAL):
    selection = json.loads((official / "selection.json").read_text(encoding="utf-8"))
    ids = selection["a1" if pool == "a1" else "d1" if pool in {"d1", "a2"} else pool]
    by_id = {row["instance_id"]: row for row in json.loads((official / "selected_public.json").read_text(encoding="utf-8"))}
    return [by_id[instance_id] for instance_id in ids]


def prepare(*, old_official: Path = OLD_OFFICIAL, official: Path = OFFICIAL):
    import pyarrow.parquet as pq

    official.mkdir(parents=True, exist_ok=True)
    for name in ("verified.parquet", "versions.json", "grader_controller.json", "grader.Dockerfile"):
        source, target = old_official / name, official / name
        if target.exists() and sha256(target) != sha256(source):
            raise ValueError(f"independent official input differs: {name}")
        if not target.exists():
            shutil.copyfile(source, target)
    parquet = official / "verified.parquet"
    if sha256(parquet) != DATASET_SHA256:
        raise ValueError("dataset does not match frozen revision")
    freeze_json(official / "resource_limits.json", {"candidate_cpus": 2, "candidate_memory": "3g",
        "parallel_episodes": 1, "candidate_slots": 3, "model_slots": 4,
        "grading_mode": "serial after candidate release", "grader_slots": 2,
        "grade_controller_memory": "768m", "source": "minimum_value_20260905_v1"})
    exposure_path = official / "exposure.json"
    inventory = exposure_inventory(old_official.parent.parent)
    freeze_json(exposure_path, inventory)
    metadata = pq.read_table(parquet, columns=["instance_id", "repo"]).to_pylist()
    selected = select_metadata(metadata, inventory["instance_ids"])
    a1, d1 = list(A1_TASKS), [row["instance_id"] for row in selected]
    selection = {"seed": SEED, "algorithm": "sha256 repository and instance ordering, round robin, max 3 per repo",
        "dataset": DATASET, "dataset_revision": DATASET_REVISION, "dataset_sha256": DATASET_SHA256,
        "population_size": len(metadata), "exposure_sha256": sha256(exposure_path),
        "a1": a1, "d1": d1, "selected": selected,
        "d1_repo_counts": dict(sorted(Counter(row["repo"] for row in selected).items()))}
    # Commit all selection decisions before reading any full dataset record.
    freeze_json(official / "selection.json", selection)
    ids = a1 + d1
    rows = pq.read_table(parquet, filters=[("instance_id", "in", ids)]).to_pylist()
    by_id = {row["instance_id"]: row for row in rows}
    if set(by_id) != set(ids):
        raise ValueError("frozen IDs missing from dataset")
    freeze_json(official / "selected_public.json", [{key: by_id[i][key] for key in PUBLIC_FIELDS} for i in ids])
    freeze_json(official / "grader" / "selected.json", [by_id[i] for i in ids])
    harness = official / "SWE-bench"
    if not harness.exists():
        subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", "--local", str(old_official / "SWE-bench"), str(harness)], check=True)
    commit = subprocess.check_output(["git", "-C", str(harness), "rev-parse", "HEAD"], text=True).strip()
    if commit != HARNESS_REVISION:
        raise ValueError("copied harness revision differs")
    return selection


def prepare_a1_checks(official: Path = OFFICIAL):
    """Verify commit-pinned PUBLIC source paths; never consult private test specs."""
    definitions = {
        "astropy__astropy-14995": ("astropy/nddata/mixins/tests/test_ndarithmetic.py", "python -m pytest -q astropy/nddata/mixins/tests/test_ndarithmetic.py", "Public issue names NDDataRef arithmetic and mask propagation."),
        "pydata__xarray-7229": ("xarray/tests/test_computation.py", "python -m pytest -q xarray/tests/test_computation.py -k where", "Public issue names xr.where and coordinate attributes."),
    }
    checks, evidence = {}, {}
    for row in load_public("a1", official):
        path, command, reason = definitions[row["instance_id"]]
        url = f"https://raw.githubusercontent.com/{row['repo']}/{row['base_commit']}/{path}"
        with urllib.request.urlopen(url, timeout=45) as response:
            content = response.read()
        if not content or b"def test" not in content:
            raise ValueError("public source is not a test file")
        checks[row["instance_id"]] = {"public_scope": command}
        evidence[row["instance_id"]] = {"path": path, "base_commit": row["base_commit"], "url": url,
            "sha256": hashlib.sha256(content).hexdigest(), "reason": reason, "timeout_seconds": 120,
            "source": "public issue and base-commit repository test file", "baseline_execution": "pending preflight"}
    freeze_json(official / "public_checks.json", checks)
    freeze_json(official / "public_checks_provenance.json", evidence)
    return checks



_COUNTS_SCRIPT = r'''
import json,sys
from pathlib import Path
from swebench.harness.test_spec.test_spec import TestSpec
from swebench.harness.grading import get_logs_eval
spec=TestSpec(**json.loads(Path('/specs.json').read_text())[sys.argv[1]])
logs=list(Path('/job').glob('logs/run_evaluation/**/test_output.txt'))
if len(logs)!=1: raise RuntimeError('expected one official evaluation log')
statuses,found=get_logs_eval(spec,str(logs[0]))
result={'parsed':found,'observed_test_count':len(statuses)}
for group in ('FAIL_TO_PASS','PASS_TO_PASS'):
    cases=getattr(spec,group)
    result[group]={'total':len(cases),'missing':sum(case not in statuses for case in cases),
        'passed':sum(statuses.get(case) in ('PASSED','XFAIL') for case in cases),
        'failed':sum(statuses.get(case) in ('FAILED','ERROR') for case in cases)}
print(json.dumps(result))
'''


def _qualification_counts(instance_id, job: Path, official: Path):
    from . import environment
    controller = json.loads((official / "grader_controller.json").read_text(encoding="utf-8"))
    result = environment._docker(["run", "--rm", "--network", "none", "--memory", "768m", "--cpus", "1",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--mount", f"type=bind,source={job.resolve()},target=/job,readonly",
        "--mount", f"type=bind,source={(official / 'grader/test_specs.json').resolve()},target=/specs.json,readonly",
        controller["image_id"], "-c", _COUNTS_SCRIPT, instance_id], timeout=120)
    return json.loads(result.stdout)


def qualification_passes(baseline, reference):
    """Missing tests cannot count as the expected baseline failure."""
    def all_present(result):
        counts = result["counts"]
        return counts["parsed"] and all(counts[group]["missing"] == 0 for group in ("FAIL_TO_PASS", "PASS_TO_PASS"))

    if not all(result["completed"] and all_present(result) for result in (baseline, reference)):
        return False
    old, new = baseline["counts"], reference["counts"]
    return (baseline["resolved"] is False and reference["resolved"] is True
        and old["FAIL_TO_PASS"]["total"] > 0 and old["FAIL_TO_PASS"]["failed"] > 0
        and old["PASS_TO_PASS"]["passed"] == old["PASS_TO_PASS"]["total"]
        and all(new[group]["passed"] == new[group]["total"] for group in ("FAIL_TO_PASS", "PASS_TO_PASS")))


def preflight_a1(official: Path = OFFICIAL, *, attempt="attempt_03"):
    """Serial no-model probes and private official baseline/reference qualification."""
    from . import environment
    if not re.fullmatch(r"attempt_[0-9]{2,}", attempt):
        raise ValueError("invalid preflight attempt name")
    contract = environment.capture_grader_contract()
    public = load_public("a1", official)
    checks = json.loads((official / "public_checks.json").read_text(encoding="utf-8"))
    private = {row["instance_id"]: row for row in json.loads((official / "grader/selected.json").read_text(encoding="utf-8"))}
    summaries = []
    for instance in public:
        instance_id = instance["instance_id"]
        directory = official / "grader" / "preflight" / instance_id / attempt
        directory.mkdir(parents=True, exist_ok=True)
        summary_path = directory / "qualification.json"
        if summary_path.exists():
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            if summary.get("grader_contract") != contract:
                raise ValueError("existing qualification uses a different frozen contract")
            summaries.append(summary)
            continue
        started = time.monotonic()
        summary = {"instance_id": instance_id, "qualified": False, "grader_contract": contract,
                   "preparation_cost_category": "no-model environment and grader preparation"}
        try:
            env = environment.ValueEnvironment(instance, directory / "candidate_probe", grader_contract=contract)
            try:
                env.start()
                commands = {}
                for check_id, command in checks[instance_id].items():
                    result = env.run(command, timeout=120)
                    commands[check_id] = {key: result[key] for key in ("exit_code", "timed_out", "duration_seconds")}
                env.run("(sleep 20; printf late > /tmp/minimum-value-late-writer) >/tmp/minimum-value-writer.log 2>&1 < /dev/null &")
                quiesce = env.quiesce()
                time.sleep(21)
                no_writer = environment._docker(["exec", env.container_id, "/usr/bin/test", "!", "-e", "/tmp/minimum-value-late-writer"], check=False)
                patch = env.export_patch()
                applicability = env.check_frozen_patch(patch)
                if no_writer.returncode or not applicability["applicable"]:
                    raise RuntimeError("candidate lifecycle probe failed")
                summary["candidate_probe"] = {"public_checks": commands, "quiesced": quiesce["quiesced"],
                    "detached_writer_stopped": True, "patch_applicable": True}
            finally:
                closed = env.close()
            if not closed["closed"]:
                raise RuntimeError("candidate probe deletion not confirmed")
            for kind in ("baseline", "reference"):
                patch = ("diff --git a/.dpswarm_baseline_qualification b/.dpswarm_baseline_qualification\n"
                    "new file mode 100644\n--- /dev/null\n+++ b/.dpswarm_baseline_qualification\n@@ -0,0 +1 @@\n+baseline qualification marker\n"
                    if kind == "baseline" else private[instance_id]["patch"])
                patch_path = directory / kind / "frozen.patch"
                patch_path.parent.mkdir(parents=True, exist_ok=True)
                content = patch.encode("utf-8")
                if patch_path.exists() and patch_path.read_bytes() != content:
                    raise ValueError("private qualification patch changed")
                patch_path.write_bytes(content)
                phase_started = time.monotonic()
                result = environment.rootgrade_terminal(instance, patch_path, hashlib.sha256(content).hexdigest(),
                    directory / kind / "evaluation", grader_contract=contract, model_name=f"preflight_{kind}", timeout=900)
                counts = _qualification_counts(instance_id, Path(result["grader_dir"]), official)
                summary[kind] = {"completed": result.get("completed") is True, "resolved": result.get("resolved"),
                    "counts": counts, "duration_seconds": time.monotonic() - phase_started}
            summary["qualified"] = qualification_passes(summary["baseline"], summary["reference"])
        except Exception as exc:
            freeze_json(directory / "private_error.json", {"error_type": type(exc).__name__, "error": str(exc)})
            summary["error_type"] = type(exc).__name__
        summary["duration_seconds"] = time.monotonic() - started
        freeze_json(summary_path, summary)
        summaries.append(summary)
        print(json.dumps({key: value for key, value in summary.items() if key != "grader_contract"}), flush=True)
    return summaries



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--public-checks", action="store_true")
    parser.add_argument("--grader-specs", action="store_true")
    parser.add_argument("--pull-a1", action="store_true")
    parser.add_argument("--preflight-a1", action="store_true")
    parser.add_argument("--preflight-attempt", default="attempt_03")
    args = parser.parse_args()
    if args.preflight_a1:
        if not all(result["qualified"] for result in preflight_a1(attempt=args.preflight_attempt)):
            raise SystemExit(1)
    if args.prepare:
        selection = prepare()
        print(json.dumps({"a1": selection["a1"], "d1": selection["d1"], "d1_repo_counts": selection["d1_repo_counts"]}))
    if args.public_checks:
        print(json.dumps({"public_checks": prepare_a1_checks()}))
    if args.grader_specs or args.pull_a1:
        from . import environment
        if args.grader_specs:
            environment.ensure_grader_specs()
            print(json.dumps({"grader_specs": "prepared"}))
        if args.pull_a1:
            for instance_id in A1_TASKS:
                result = environment.ensure_image(instance_id)
                print(json.dumps({"instance_id": instance_id, "image_id": result["image_id"]}))


if __name__ == "__main__":
    main()
