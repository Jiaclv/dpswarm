"""Create a deterministic, content-addressed npm archive from the declared package files."""
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import tarfile

root = Path(sys.argv[1]).resolve()
output = Path(sys.argv[2]).resolve()
manifest = json.loads((root / "package.json").read_text(encoding="utf-8"))
entries = {Path("package.json")}
for item in manifest["files"]:
    candidate = root / item
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root) or candidate.is_symlink():
        raise ValueError("Package file must remain inside the source directory")
    if candidate.is_dir():
        entries.update(p.relative_to(root) for p in candidate.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")
    elif candidate.is_file():
        entries.add(candidate.relative_to(root))
    else:
        raise ValueError(f"Missing declared package file: {item}")
files = []
hash_ = hashlib.sha256()
for relative in sorted(entries, key=lambda p: p.as_posix()):
    candidate = root / relative
    if candidate.is_symlink() or not candidate.resolve().is_relative_to(root):
        raise ValueError("Package links outside the source directory are not supported")
    data = candidate.read_bytes()
    name = relative.as_posix()
    hash_.update(name.encode("utf-8") + b"\0" + str(len(data)).encode("ascii") + b"\0" + data)
    files.append((name, data))
name = f"{manifest['name']}-{manifest['version']}"
if not re.fullmatch(r"[a-zA-Z0-9_.-]+", name):
    raise ValueError("Unsupported package filename")
# Fixed metadata makes identical input bytes produce the same immutable archive.
buffer = io.BytesIO()
with gzip.GzipFile(fileobj=buffer, mode="wb", filename="", mtime=0) as compressed:
    with tarfile.open(fileobj=compressed, mode="w") as archive:
        for relative, data in files:
            info = tarfile.TarInfo("package/" + relative)
            info.size = len(data)
            info.mode = 0o755 if relative == "bin/setup.mjs" else 0o644
            archive.addfile(info, io.BytesIO(data))
data = buffer.getvalue()
output.mkdir(parents=True, exist_ok=True)
path = output / f"{name}-{hash_.hexdigest()[:20]}.tgz"
if path.exists():
    if path.read_bytes() != data:
        raise ValueError("Existing content-addressed archive has different bytes")
else:
    with path.open("xb") as target:
        target.write(data)
print(json.dumps({"archive": str(path), "source_sha256": hash_.hexdigest(), "files": len(files)}))
