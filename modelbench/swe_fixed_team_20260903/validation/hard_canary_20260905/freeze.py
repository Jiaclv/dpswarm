"""Freeze this one preregistered live canary after its fresh no-model checks."""
from pathlib import Path
import json
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from modelbench.swe_fixed_team_20260903 import cli
from modelbench.swe_fixed_team_20260903.runner import LIMITS, utc

HERE = Path(__file__).resolve().parent
GATE = HERE.parent / 'gate_revision12_hard_canary_20260905.json'
BATCH = cli.HERE / 'hard_canary_20260905'


def main():
    assert not GATE.exists() and not BATCH.exists(), 'Never overwrite a frozen experiment'
    environment = cli.read(HERE / 'environment/result.json')
    assert environment['status'] == 'PASS' and environment['model_calls'] == 0
    assert environment['grader_contract']['environment_sha'] == cli.sources()[cli.ENV_SOURCE]
    assert environment['grader_contract']['input_artifacts'] == cli.input_artifacts()
    score = environment['negative_control']
    assert score['completed'] is True and score['resolved'] is False
    assert score['process_exit_code'] == 0 and not score['cleanup_errors']
    tests = []
    for origin, name in [
        ('test-artifacts/engineering-20260905/environment-entrypoint/junit.xml', 'environment-entrypoint.junit.xml'),
        ('test-artifacts/engineering-20260905/explicit-gate/final.xml', 'explicit-gate.junit.xml'),
    ]:
        target = HERE / name
        assert not target.exists()
        shutil.copyfile(ROOT / origin, target)
        suites = ET.parse(target).getroot().iter('testsuite')
        counts = {key: 0 for key in ('tests', 'failures', 'errors', 'skipped')}
        for suite in suites:
            for key in counts:
                counts[key] += int(suite.attrib.get(key, 0))
        assert counts['tests'] and not any(counts[k] for k in ('failures','errors','skipped')), counts
        tests.append({'artifact': name, **counts})
    from modelbench.team_eval20260903.transports import _keyconfig
    assert _keyconfig('GLM_API_KEY') and _keyconfig('DEEPSEEK_API_KEY')
    docker = json.loads(subprocess.check_output(['docker','info','--format','{{json .}}'], text=True))
    host = {k: docker[k] for k in ('ServerVersion','OSType','OperatingSystem','NCPU','MemTotal')}
    host.update(glm_key_present=True, deepseek_key_present=True,
                codex_version=subprocess.check_output(['codex.cmd','--version'], text=True).strip())
    (HERE / 'host-preflight.json').write_text(json.dumps(host,indent=2)+'\n', encoding='utf-8')
    prior = cli.read(cli.GATE_PATH)
    changed = {k: {'offline_sha256': prior['runtime_sources'].get(k), 'current_sha256': v}
               for k,v in cli.sources().items() if prior['runtime_sources'].get(k) != v}
    artifacts = {p.relative_to(HERE.parent).as_posix(): cli.sha(p)
                 for p in sorted(HERE.rglob('*')) if p.is_file() and '__pycache__' not in p.parts}
    gate = {'revision': 12, 'status': 'PASS', 'created_at': utc(), 'live_run_admission': True,
        'scope': 'One exposed-task hard canary after fresh container and official negative control; not the full P0/P1 matrix',
        'allowed_schedule': [{'instance_id':'pydata__xarray-7229',
                              'arm':'hetero_gpt-5.6-terra__glm-5.3', 'repetitions':1}],
        'requested_roles': {'lead':'gpt-5.6-sol','production':'gpt-5.6-terra','tests':'glm-5.3','cm':'deepseek-v4-flash'},
        'effective_limits': LIMITS, 'plan': 'hard_canary_20260905/PLAN.md',
        'runtime_sources': cli.sources(), 'input_artifacts': cli.input_artifacts(),
        'validation_artifacts': artifacts, 'test_counts': tests,
        'prior_offline_gate': {'path':str(cli.GATE_PATH),'sha256':cli.sha(cli.GATE_PATH),
                               'status':prior['status'],'note':'Old tests cover the recorded old bytes; current entrypoint and gate changes have new targeted checks.'},
        'changes_after_offline_gate': changed,
        'negative_control_completed':True, 'negative_control_resolved':False,
        'negative_control_reused':False, 'model_preflight_calls':0,
        'physical_preflight': environment, 'host':host,
        'monetary_hard_limit_supported':False, 'full_P0_completed':False,
        'coverage_exclusions':['assembly paths disabled','DSH bridge','autonomous delegation','multi-arm causal comparison']}
    GATE.write_text(json.dumps(gate,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    manifest = cli.prepare(BATCH, only_instances=['pydata__xarray-7229'],
        only_arms=['hetero_gpt-5.6-terra__glm-5.3'], wave='hetero16', validation_gate_path=GATE)
    assert len(manifest['schedule']) == 1
    entry = manifest['schedule'][0]
    assert entry['lead_model'] == 'gpt-5.6-sol' and entry['worker_models'] == ['gpt-5.6-terra','glm-5.3']
    assert entry['effective_limits'] == LIMITS
    cli.verify(BATCH)
    print(json.dumps({'batch': str(BATCH), 'gate': str(GATE), 'scheduled_runs': 1,
        'manifest_sha256': cli.sha(BATCH/'manifest.json'), 'source_changes':list(changed), 'tests':tests}, indent=2))


if __name__ == '__main__':
    main()
