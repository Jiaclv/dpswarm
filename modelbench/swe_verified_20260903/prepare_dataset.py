"""Freeze metadata-only SWE-bench Verified sampling and official artifacts.

Selection never reads problem text, difficulty, gold patches, or expected tests.
Private grading records are written only after the selected IDs are fixed.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import subprocess
import urllib.request

ROOT = Path(__file__).resolve().parent
OFFICIAL = ROOT / "official"
DATASET = "princeton-nlp/SWE-bench_Verified"
DATASET_REVISION = "c104f840cc67f8b6eec6f759ebc8b2693d585d4a"
DATASET_SHA256 = "a45b1fe4e2f0c8390b2b2938ac83e92ed5979000856808f3679c07812e9e6dcd"
HARNESS_REVISION = "726c5461e2ef52d83cf1ea2107870a8bb3328d57"
PUBLIC_FIELDS = ("instance_id", "repo", "base_commit", "problem_statement", "version")


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def select_metadata(rows: list[dict], count: int = 10, seed: str = "20260903") -> list[dict]:
    """One sample per repository; hash chooses both strata and member order."""
    if count < 1:
        raise ValueError("count must be positive")
    metadata = [{"instance_id": r["instance_id"], "repo": r["repo"]} for r in rows]
    if len({r["instance_id"] for r in metadata}) != len(metadata):
        raise ValueError("duplicate instance IDs")
    digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
    repos = sorted({r["repo"] for r in metadata}, key=lambda r: (digest(f"{seed}:repo:{r}"), r))
    if count > len(repos):
        raise ValueError("one-per-repo design has fewer strata than requested samples")
    return [min((r for r in metadata if r["repo"] == repo),
                key=lambda r: (digest(f"{seed}:instance:{r['instance_id']}"), r["instance_id"]))
            for repo in repos[:count]]


def prepare(count: int = 10, seed: str = "20260903") -> dict:
    import pyarrow.parquet as pq
    OFFICIAL.mkdir(parents=True, exist_ok=True)
    parquet = OFFICIAL / "verified.parquet"
    if not parquet.exists():
        url = f"https://huggingface.co/datasets/{DATASET}/resolve/{DATASET_REVISION}/data/test-00000-of-00001.parquet"
        temporary = parquet.with_suffix(".download")
        urllib.request.urlretrieve(url, temporary)
        temporary.replace(parquet)
    actual_sha256 = hashlib.sha256(parquet.read_bytes()).hexdigest()
    if actual_sha256 != DATASET_SHA256:
        raise ValueError("frozen official dataset hash mismatch")
    metadata = pq.read_table(parquet, columns=["instance_id", "repo"]).to_pylist()
    selected = select_metadata(metadata, count, seed)
    selection = {"seed": seed, "algorithm": "sha256 repo order, first N strata, one minimum sha256 instance in each",
                 "dataset": DATASET, "dataset_revision": DATASET_REVISION,
                 "dataset_sha256": actual_sha256,
                 "population_size": len(metadata), "selected": selected,
                 "repo_counts": {r: sum(x["repo"] == r for x in metadata) for r in sorted({x["repo"] for x in metadata})}}
    prior = OFFICIAL / "selection.json"
    if prior.exists() and json.loads(prior.read_text(encoding="utf-8"))["selected"] != selected:
        raise ValueError("existing frozen selection differs; refusing to replace it")
    write_json(prior, selection)
    # Only now read selected full records; do not print private contents.
    ids = [r["instance_id"] for r in selected]
    records = pq.read_table(parquet, filters=[("instance_id", "in", ids)]).to_pylist()
    by_id = {r["instance_id"]: r for r in records}
    public = [{key: by_id[i][key] for key in PUBLIC_FIELDS} for i in ids]
    write_json(OFFICIAL / "selected_public.json", public)
    write_json(OFFICIAL / "grader" / "selected.json", [by_id[i] for i in ids])
    harness = OFFICIAL / "SWE-bench"
    revision = subprocess.check_output(["git", "-C", str(harness), "rev-parse", "HEAD"], text=True).strip()
    if revision != HARNESS_REVISION:
        raise ValueError(f"official harness commit mismatch: {revision}")
    write_json(OFFICIAL / "versions.json", {"harness_repo": "https://github.com/SWE-bench/SWE-bench",
        "harness_tag": "v4.1.0", "harness_commit": revision, "dataset": DATASET,
        "dataset_revision": DATASET_REVISION, "dataset_sha256": selection["dataset_sha256"],
        "reason": "Official v4.1.0 supports the frozen original Verified schema; v5 uses a different task schema."})
    return selection


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--seed", default="20260903")
    parser.add_argument("--pull", nargs="*", default=[], help="Selected instance IDs; at most two pulls concurrently")
    args = parser.parse_args()
    selection = prepare(args.count, args.seed)
    print(json.dumps({"selected": selection["selected"], "revision": DATASET_REVISION}))
    if args.pull:
        from environment import ensure_image, SWEEnvironment
        public = {r["instance_id"]: r for r in json.loads((OFFICIAL / "selected_public.json").read_text(encoding="utf-8"))}
        if set(args.pull) - set(public):
            raise ValueError("requested image is outside the frozen selection")
        def probe(instance_id):
            info = ensure_image(instance_id)
            env = SWEEnvironment(public[instance_id], OFFICIAL / "probes" / instance_id, memory="3g")
            try:
                env.start()
                result = env.run("python --version && git rev-parse HEAD && test ! -e /var/run/docker.sock")
                info["probe"] = result
            finally:
                env.close()
            write_json(OFFICIAL / "images" / f"{instance_id}.json", info)
            return {"instance_id": instance_id, "image_id": info["image_id"], "probe": result}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(probe, i): i for i in args.pull}
            for future in as_completed(futures):
                try:
                    print(json.dumps(future.result()))
                except Exception as exc:
                    print(json.dumps({"instance_id": futures[future], "environment_error": str(exc)}))


if __name__ == "__main__":
    main()
