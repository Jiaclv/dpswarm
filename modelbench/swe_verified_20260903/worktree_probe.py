"""Content observation without staging, filters, repository hooks or patch execution."""

# Executed by the environment's existing restricted Python interpreter. Git only
# enumerates tracked and non-ignored paths; bytes are read directly, including
# symlink targets rather than following them. No index or worktree is mutated.
PROBE_SCRIPT = r'''
import hashlib, json, os, stat, subprocess
paths = subprocess.check_output([
    'git', '-c', 'core.fsmonitor=false', 'ls-files', '-z',
    '--cached', '--others', '--exclude-standard'])
files = {}
for raw in sorted(set(paths.split(b'\0')) - {b''}):
    name = os.fsdecode(raw)
    try:
        mode = os.lstat(name).st_mode
        if stat.S_ISLNK(mode):
            data = os.fsencode(os.readlink(name))
            files[name] = {'kind': 'symlink', 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        elif stat.S_ISREG(mode):
            digest = hashlib.sha256()
            size = 0
            with open(name, 'rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
                    size += len(chunk)
            files[name] = {'kind': 'file', 'bytes': size, 'sha256': digest.hexdigest()}
        elif stat.S_ISDIR(mode):
            files[name] = {'kind': 'directory'}
        else:
            raise RuntimeError('Unsupported tracked path type: ' + name)
    except FileNotFoundError:
        pass
print(json.dumps(files, ensure_ascii=True, sort_keys=True))
'''
