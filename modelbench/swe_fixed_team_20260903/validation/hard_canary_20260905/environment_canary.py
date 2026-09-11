"""One fresh xarray environment check and official non-solution negative control."""
from pathlib import Path
import hashlib
import json
import shlex
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from modelbench.swe_verified_20260903.environment import (
    OFFICIAL, SWEEnvironment, capture_grader_contract, inspect_image, image_name,
)

OUT = Path(__file__).resolve().parent / 'environment'
INSTANCE = 'pydata__xarray-7229'


def save(name, value):
    path = OUT / name
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def main():
    OUT.mkdir(exist_ok=False)
    started = time.time()
    public = json.loads((OFFICIAL / 'selected_public.json').read_text(encoding='utf-8'))
    instance = next(x for x in public if x['instance_id'] == INSTANCE)
    frozen_image = json.loads((OFFICIAL / 'images' / (INSTANCE + '.json')).read_text(encoding='utf-8'))
    actual = inspect_image(image_name(INSTANCE))
    assert actual and actual['Id'] == frozen_image['image_id'], 'Image does not match frozen digest'
    contract = capture_grader_contract()
    before_inputs = dict(contract['input_artifacts'])
    env = SWEEnvironment(instance, OUT / 'candidate', image=image_name(INSTANCE),
                         cpus=2, memory='3g', grader_contract=contract)
    checks = []

    def execute(code):
        result = env.run('python -I -c ' + shlex.quote(code))
        assert result['exit_code'] == 0 and not result['timed_out'], result
        return result['stdout'].strip()

    def observe(label):
        checksum = "import hashlib; print(hashlib.sha256(open('.git/index','rb').read()).hexdigest())"
        before = execute(checksum)
        at = time.perf_counter()
        value = env.observe_worktree()
        elapsed = time.perf_counter() - at
        after = execute(checksum)
        assert before == after, 'Probe modified git index'
        checks.append({'name': label, 'elapsed_seconds': elapsed, 'index_unchanged': True,
                       'observation': value})
        save('probe-checks.json', checks)
        return value

    try:
        env.start()
        isolation = json.loads(execute("import os,json; print(json.dumps({'uid':os.getuid(),'docker_socket':os.path.exists('/var/run/docker.sock'),'eval_script':os.path.exists('/eval.sh')}))"))
        assert isolation == {'uid': 1000, 'docker_socket': False, 'eval_script': False}, isolation
        save('isolation.json', isolation)
        baseline = observe('clean-baseline')
        assert not baseline['nonempty_delta']
        execute("from pathlib import Path; Path('dpswarm_probe.txt').write_text('probe\\n')")
        add = observe('text-addition')
        assert add['changed_files']['dpswarm_probe.txt']['before'] is None
        execute("from pathlib import Path; Path('dpswarm_probe.bin').write_bytes(b'\\x00\\xff')")
        binary = observe('binary-addition')
        assert binary['changed_files']['dpswarm_probe.bin']['after']['sha256'] == hashlib.sha256(b'\x00\xff').hexdigest()
        deleted = execute("import subprocess,json; from pathlib import Path; names=subprocess.check_output(['git','ls-files','-z']).decode().split('\\0'); p=next(Path(n) for n in names if n and Path(n).is_file() and not Path(n).is_symlink()); Path('/tmp/dpswarm-deletion-backup').write_bytes(p.read_bytes()); Path('/tmp/dpswarm-deletion-path').write_text(str(p)); p.unlink(); print(str(p))")
        deletion = observe('tracked-file-deletion')
        assert deletion['changed_files'][deleted]['after'] is None
        execute("from pathlib import Path; p=Path(Path('/tmp/dpswarm-deletion-path').read_text()); p.write_bytes(Path('/tmp/dpswarm-deletion-backup').read_bytes()); Path('dpswarm_probe.txt').unlink(); Path('dpswarm_probe.bin').unlink()")
        reverted = observe('fully-reverted')
        assert not reverted['nonempty_delta'] and reverted['state_sha256'] == baseline['state_sha256']
        execute("from pathlib import Path; Path('json.py').write_text('raise RuntimeError(\"probe must not import workspace json\")\\n')")
        shadow = observe('workspace-module-shadow')
        assert 'json.py' in shadow['changed_files']
        execute("from pathlib import Path; Path('json.py').unlink()")
        restored = observe('restored-before-negative-control')
        assert restored['state_sha256'] == baseline['state_sha256']
        execute("from pathlib import Path; Path('dpswarm_environment_probe.txt').write_text('Non-solution environment validation only.\\n')")
        patch = env.export_patch()
        assert patch.strip() and 'dpswarm_environment_probe.txt' in patch
        save('negative-control-start.json', {'time': time.time(), 'patch_sha256': hashlib.sha256(patch.encode()).hexdigest()})
        print('Observation checks passed; starting official negative control', flush=True)
        score = env.grade(patch, model_name='hard-canary-environment-negative-control', timeout=900)
        assert score.get('completed') is True and score.get('resolved') is False, score
        assert score.get('process_exit_code') == 0 and score.get('cleanup_errors') == [], score
        assert not score.get('infrastructure_error'), score
        assert capture_grader_contract()['input_artifacts'] == before_inputs, 'Grader input drift'
        save('result.json', {'status': 'PASS', 'instance_id': INSTANCE, 'started_epoch': started,
            'elapsed_seconds': time.time() - started, 'image_id': actual['Id'],
            'grader_contract': contract, 'isolation': isolation, 'probe_checks': len(checks),
            'probe_max_seconds': max(x['elapsed_seconds'] for x in checks),
            'negative_control': score, 'model_calls': 0})
        print('Environment and official negative control PASS', flush=True)
    except BaseException as exc:
        save('failure.json', {'type': type(exc).__name__, 'message': str(exc), 'traceback': traceback.format_exc()})
        raise
    finally:
        env.close()


if __name__ == '__main__':
    main()
