"""Reclaim idle Docker WSL file cache before a new wave, without deleting data."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time

import psutil


def _run(argv):
    return subprocess.run(argv, capture_output=True, text=True, encoding='utf-8',
        errors='replace', timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def _idle(lock, run):
    if Path(lock).exists():
        raise RuntimeError('Resource lease is held; cache operation not admitted')
    result = run(['docker', 'ps', '--quiet'])
    if result.returncode != 0 or result.stdout.strip():
        raise RuntimeError('Docker is unavailable or has running containers')


def ensure_idle_headroom(receipt_path, resource_lock_path, minimum_gib=4,
                         *, run=_run, available=lambda: psutil.virtual_memory().available,
                         sleep=time.sleep, clock=time.monotonic):
    path = Path(receipt_path)
    if path.exists():
        raise ValueError('An idle-memory receipt already exists')
    threshold = minimum_gib * 2**30
    record = {'started_at': datetime.now(timezone.utc).isoformat(),
              'minimum_available_gib': minimum_gib, 'cache_reclaimed': False,
              'files_deleted': False, 'models_started': 0, 'status': 'checking', 'ready': False}
    try:
        _idle(resource_lock_path, run)
        record['host_available_before_bytes'] = available()
        if record['host_available_before_bytes'] < threshold:
            info = run(['wsl.exe', '-d', 'docker-desktop', '-u', 'root', '--', 'cat', '/proc/meminfo'])
            if info.returncode != 0:
                raise RuntimeError('Cannot inspect Docker WSL memory')
            values = {}
            for line in info.stdout.splitlines():
                key, _, rest = line.partition(':')
                if key in ('MemTotal', 'MemAvailable', 'Cached', 'Buffers'):
                    values[key] = int(rest.split()[0]) * 1024
            record['wsl_before'] = values
            _idle(resource_lock_path, run)
            result = run(['wsl.exe', '-d', 'docker-desktop', '-u', 'root', '--',
                          'sh', '-c', 'sync; echo 3 > /proc/sys/vm/drop_caches'])
            if result.returncode != 0:
                raise RuntimeError('Idle WSL cache reclaim failed')
            record['cache_reclaimed'] = True
            deadline = clock() + 45
            while available() < threshold and clock() < deadline:
                sleep(3)
        _idle(resource_lock_path, run)
        record['host_available_after_bytes'] = available()
        if record['host_available_after_bytes'] < threshold:
            raise RuntimeError('Host memory remains below the new-wave admission floor')
        record['status'] = 'ready'
        record['ready'] = True
        return record
    except BaseException as exc:
        record.update(status='blocked', error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        record['finished_at'] = datetime.now(timezone.utc).isoformat()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('x', encoding='utf-8') as stream:
            json.dump(record, stream, ensure_ascii=True, indent=2)
            stream.write('\n')
