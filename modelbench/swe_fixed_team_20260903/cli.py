"""Freeze and execute the user's forced-team pilot without changing old runs."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from datetime import datetime
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import time
from uuid import uuid4

from .runner import SweRun, LIMITS, MODELS, dump, utc
from .runtime_integrity import resolve_limits
from .reporting import report
from ..swe_verified_20260903.environment import ensure_image, cleanup_image, image_name

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PRIOR = REPO / 'modelbench/swe_verified_20260903'
OFFICIAL = PRIOR / 'official'
ENV_SOURCE = 'modelbench/swe_verified_20260903/environment.py'
# Revision 12 records the current engineering validation. Historical gates and
# frozen batches remain immutable; offline checks do not authorize live runs.
GATE_PATH = HERE / 'validation/gate_revision12_test_evidence_20260905.json'
INPUT_NAMES = ('versions.json', 'selection.json', 'selected_public.json', 'verified.parquet',
    'grader_controller.json', 'grader.Dockerfile', 'resource_limits.json',
    'grader/test_specs_provenance.json', 'grader/test_specs.json', 'grader/selected.json')
TASK_IDS = ['matplotlib__matplotlib-25122', 'sphinx-doc__sphinx-8035']
# NEXT_EXPERIMENT_PLAN_20260904 §2/§3. Pair order interleaves arms so repeated
# runs of one arm never share a pair (same-arm repeats run staggered, §4.4).
ABLATION9_OVERRIDE = {'cm_team_memory': False, 'cm_scout_distill': False, 'cm_bootstrap_package': False}
# rev10 turned assembly off by default; the rev9 ablation wave pins its ON arm
# explicitly so the wave keeps its frozen meaning regardless of the defaults.
ASSEMBLY9_ON = {'cm_team_memory': True, 'cm_scout_distill': True, 'cm_bootstrap_package': True}
BUDGET11_OVERRIDE = {'max_calls': 40, 'token_limit': 900_000, 'worker_calls': 16, 'cm_call_allowance': 12}
REV9_WAVES = {
    'ablation9': [('solo_gpt-5.6-sol', 1), ('hetero_gpt-5.6-terra__glm-5.3', 1),
                  ('solo_gpt-5.6-sol', 2), ('hetero_gpt-5.6-terra__glm-5.3', 2),
                  ('solo_gpt-5.6-sol', 3), ('heterooff_gpt-5.6-terra__glm-5.3', 1),
                  ('hetero_gpt-5.6-terra__glm-5.3', 3), ('heterooff_gpt-5.6-terra__glm-5.3', 2),
                  ('heterooff_gpt-5.6-terra__glm-5.3', 3)],
    'budget11': [('fixed_glm-5.3', 1), ('hetero_gpt-5.6-terra__glm-5.3', 1),
                 ('fixed_glm-5.3', 2), ('hetero_gpt-5.6-terra__glm-5.3', 2),
                 ('fixed_gpt-5.6-sol', 1), ('fixed_deepseek-v4-flash', 1),
                 ('fixed_gpt-5.6-sol', 2)],
}
# NEXT_EXPERIMENT_PLAN_20260904B §2: CM-pool scan on sphinx-8035. Display arms
# carry the pool suffix; the base arm drives entry decoding. Pool-12 baselines
# reuse pilot_v10 data and are not re-run. Interleaved so no pair holds two
# runs of one arm (staggered repeats, plan §4.4 of 20260904).
CMSCAN13 = [
    ('solo_gpt-5.6-sol.cm6', 'solo_gpt-5.6-sol', 1, {'cm_call_allowance': 6}),
    ('solo_gpt-5.6-sol.cm24', 'solo_gpt-5.6-sol', 1, {'cm_call_allowance': 24}),
    ('solo_gpt-5.6-sol.cm6', 'solo_gpt-5.6-sol', 2, {'cm_call_allowance': 6}),
    ('solo_gpt-5.6-sol.cm24', 'solo_gpt-5.6-sol', 2, {'cm_call_allowance': 24}),
    ('heterooff_gpt-5.6-terra__glm-5.3.cm6', 'heterooff_gpt-5.6-terra__glm-5.3', 1, {'cm_call_allowance': 6}),
    ('heterooff_gpt-5.6-terra__glm-5.3.cm24', 'heterooff_gpt-5.6-terra__glm-5.3', 1, {'cm_call_allowance': 24}),
]
# NEXT_EXPERIMENT_PLAN_20260904B §3: probe panel = every frozen instance except
# the two anchored tasks (sphinx-8035 hard, astropy-14995 easy), one solo probe
# each. Pre-registered: no probe result may influence the selection. The 8th
# candidate is matplotlib-25122 (started once under the pre-rev3 protocol in
# pilot_v2; that history is old-protocol data and leaks no rev9 probe signal).
PROBE_ARM = 'solo_gpt-5.6-sol'
PROBE_ANCHORS = ('sphinx-doc__sphinx-8035', 'astropy__astropy-14995')
PROBE_PANEL_RULE = ('Pre-registered (NEXT_EXPERIMENT_PLAN_20260904B section 3): all frozen instances '
                    'except the two anchored tasks, one solo_gpt-5.6-sol probe run each; repo-stratified '
                    'subsampling activates only if candidates exceed the 8 slots (they do not); '
                    'selection is independent of any probe outcome')
# NEXT_EXPERIMENT_PLAN_20260904C section 2: solo hard-tier confirmation wave
# under rev11 defaults. Tuples are (instance, display arm, rep, override); the
# base arm for entry decoding drops the mechanism suffix. The sphinx block
# interleaves default / curfew-OFF / pool-24 so repeats of one arm never share
# a pair (staggered repeats); instance blocks stay contiguous so each image is
# built and cleaned up once.
CURFEW15 = [
    ('sphinx-doc__sphinx-8035', 'solo_gpt-5.6-sol', 1, None),
    ('sphinx-doc__sphinx-8035', 'solo_gpt-5.6-sol.nocurf', 1, {'cm_edit_curfew': False}),
    ('sphinx-doc__sphinx-8035', 'solo_gpt-5.6-sol.cm24', 1, {'cm_call_allowance': 24}),
    ('sphinx-doc__sphinx-8035', 'solo_gpt-5.6-sol', 2, None),
    ('sphinx-doc__sphinx-8035', 'solo_gpt-5.6-sol.nocurf', 2, {'cm_edit_curfew': False}),
    ('sphinx-doc__sphinx-8035', 'solo_gpt-5.6-sol.cm24', 2, {'cm_call_allowance': 24}),
    ('sphinx-doc__sphinx-8035', 'solo_gpt-5.6-sol', 3, None),
    ('pydata__xarray-7229', 'solo_gpt-5.6-sol', 1, None),
    ('pydata__xarray-7229', 'solo_gpt-5.6-sol', 2, None),
    ('mwaskom__seaborn-3069', 'solo_gpt-5.6-sol', 1, None),
    ('mwaskom__seaborn-3069', 'solo_gpt-5.6-sol', 2, None),
    ('scikit-learn__scikit-learn-25232', 'solo_gpt-5.6-sol', 1, None),
    ('sympy__sympy-16792', 'solo_gpt-5.6-sol', 1, None),
    ('astropy__astropy-14995', 'solo_gpt-5.6-sol', 1, None),
]
CURFEW15_RULE = ('Pre-registered (NEXT_EXPERIMENT_PLAN_20260904C section 2): hard-tier sphinx 3 reps '
                 'interleaved with curfew-OFF and pool-24 ablations, plus xarray/seaborn/sklearn/sympy/astropy '
                 'solo probes under rev11 defaults; sympy probes F1 downside on a saturated-compression pass, '
                 'astropy is the easy-tier regression; selection is independent of any run outcome')
# NEXT_EXPERIMENT_PLAN_20260904C section 3: fixed-team hard-tier wave (defined
# now, prepared for pilot_v16 later). Review revision: every team arm pins
# edit_status_banner=False because the integrating Lead never trips edit
# detection and the F5 banner would misreport for the whole run; the ON arm
# pins assembly exactly as the rev9 ablation wave pinned its frozen meaning.
TEAMHARD16 = [
    ('pydata__xarray-7229', 'heterooff_gpt-5.6-terra__glm-5.3', 1, {'edit_status_banner': False}),
    ('pydata__xarray-7229', 'hetero_gpt-5.6-terra__glm-5.3', 1, {**ASSEMBLY9_ON, 'edit_status_banner': False}),
    ('pydata__xarray-7229', 'heterooff_gpt-5.6-terra__glm-5.3', 2, {'edit_status_banner': False}),
    ('pydata__xarray-7229', 'hetero_gpt-5.6-terra__glm-5.3', 2, {**ASSEMBLY9_ON, 'edit_status_banner': False}),
    ('mwaskom__seaborn-3069', 'heterooff_gpt-5.6-terra__glm-5.3', 1, {'edit_status_banner': False}),
    ('mwaskom__seaborn-3069', 'hetero_gpt-5.6-terra__glm-5.3', 1, {**ASSEMBLY9_ON, 'edit_status_banner': False}),
    ('mwaskom__seaborn-3069', 'heterooff_gpt-5.6-terra__glm-5.3', 2, {'edit_status_banner': False}),
    ('mwaskom__seaborn-3069', 'hetero_gpt-5.6-terra__glm-5.3', 2, {**ASSEMBLY9_ON, 'edit_status_banner': False}),
    ('sphinx-doc__sphinx-8035', 'heterooff_gpt-5.6-terra__glm-5.3', 1, {'edit_status_banner': False}),
    ('sphinx-doc__sphinx-8035', 'heterooff_gpt-5.6-terra__glm-5.3', 2, {'edit_status_banner': False}),
]
TEAMHARD16_RULE = ('Pre-registered (NEXT_EXPERIMENT_PLAN_20260904C section 3): terra-implementation x '
                   'glm-test fixed teams on xarray/seaborn with assembly ON/OFF, plus a sphinx heterooff '
                   'regression canary; all team arms pin edit_status_banner=False per the review revision; '
                   'selection is independent of any run outcome')
# NEXT_EXPERIMENT_PLAN_20260904C section 5, B-branch (user decision after the
# pilot_v15 audit): the pool-24 arm died budget_exhausted with the CM pool
# unexhausted, so the competing budget hypothesis gets its own pre-registered
# wave at 900k tokens / 40 calls before teamhard16 unfreezes.
BUDGET15B = [
    ('sphinx-doc__sphinx-8035', 'solo_gpt-5.6-sol.b900', 1, {'max_calls': 40, 'token_limit': 900000}),
    ('sphinx-doc__sphinx-8035', 'solo_gpt-5.6-sol.b900', 2, {'max_calls': 40, 'token_limit': 900000}),
    ('pydata__xarray-7229', 'solo_gpt-5.6-sol.b900', 1, {'max_calls': 40, 'token_limit': 900000}),
    ('pydata__xarray-7229', 'solo_gpt-5.6-sol.b900', 2, {'max_calls': 40, 'token_limit': 900000}),
]
BUDGET15B_RULE = ('Pre-registered B-branch of NEXT_EXPERIMENT_PLAN_20260904C section 5, triggered by '
                  'pilot_v15 P3 outcome (pool-24 arm 0/2 with pool unexhausted at budget_exhausted death); '
                  'tests the competing budget hypothesis at 900k tokens / 40 calls; selection independent '
                  'of run outcomes')

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def sources():
    files = list(HERE.glob('*.py')) + [HERE / 'PLAN.md'] + list((HERE / 'tests').rglob('*.py'))
    files += list(PRIOR.glob('*.py')) + list((PRIOR / 'tests').glob('*.py'))
    files += list((REPO / 'dpswarm-plugin/dpswarm').rglob('*.py'))
    files += [REPO / 'modelbench/team_runtime_v2/transport.py', REPO / 'modelbench/team_eval20260903/transports.py']
    return {p.relative_to(REPO).as_posix(): sha(p) for p in sorted(set(files))}

def input_artifacts():
    return {name: sha(OFFICIAL / name) for name in INPUT_NAMES}

def rev9_entry(arm, lead_model_default):
    """Decode one rev9 wave arm into a schedule entry body plus its override."""
    if arm.startswith('heterooff_'):
        pair = arm.removeprefix('heterooff_').split('__')
        return ({'condition': 'hetero_team', 'worker_model': None,
                 'worker_models': pair, 'lead_model': lead_model_default}, dict(ABLATION9_OVERRIDE))
    if arm.startswith('solo_'):
        return {'condition': 'solo', 'worker_model': None, 'worker_models': [],
                'lead_model': arm.removeprefix('solo_')}, None
    if arm.startswith('hetero_'):
        pair = arm.removeprefix('hetero_').split('__')
        return {'condition': 'hetero_team', 'worker_model': None,
                'worker_models': pair, 'lead_model': lead_model_default}, None
    worker_model = arm.removeprefix('fixed_')
    return {'condition': 'fixed_team', 'worker_model': worker_model,
            'worker_models': [worker_model] * 2, 'lead_model': lead_model_default}, None


def _verify_gate_artifacts(gate, root=None):
    """Check declared validation evidence before creating or starting a batch."""
    root = (Path(root) if root is not None else HERE / 'validation').resolve()
    artifacts = gate.get('validation_artifacts', {})
    if not isinstance(artifacts, dict):
        raise ValueError('Gate validation_artifacts must be a mapping')
    for relative, expected in artifacts.items():
        if not isinstance(relative, str):
            raise ValueError('Gate artifact path must be relative text')
        path = (root / relative).resolve()
        if Path(relative).is_absolute() or not path.is_relative_to(root):
            raise ValueError('Gate artifact path escapes validation directory')
        if not path.is_file() or sha(path) != expected:
            raise ValueError('Gate validation artifact missing or changed: ' + relative)


def _validate_runtime_entries(manifest):
    """Bind effective settings and exact public/grader inputs before resources."""
    public = {row['instance_id']: row for row in read(OFFICIAL / 'selected_public.json')}
    contract = {'input_artifacts': manifest['input_artifacts'],
                'environment_sha': manifest['runtime_sources'][ENV_SOURCE]}
    for entry in manifest['schedule']:
        effective = resolve_limits(LIMITS, entry.get('limits_override'))
        if entry.get('effective_limits') != effective:
            raise ValueError('Gate runtime effective_limits disagree with limits_override')
        config = {k: entry.get(k) for k in
                  ('condition', 'lead_model', 'worker_model', 'worker_models', 'effective_limits')}
        expected = hashlib.sha256(json.dumps(config, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        if entry.get('configuration_sha256') != expected:
            raise ValueError('Gate runtime configuration hash differs from entry')
        instance = entry.get('instance')
        if not isinstance(instance, dict) or instance != public.get(instance.get('instance_id')):
            raise ValueError('Gate runtime instance differs from frozen public input')
        if entry.get('grader_contract') != contract:
            raise ValueError('Gate runtime grader contract differs from frozen source and inputs')


def _validate_gate_scope(gate, schedule, inputs):
    """Enforce scope already declared by a gate; legacy unscoped gates stay readable."""
    if 'live_run_admission' in gate and gate['live_run_admission'] is not True:
        raise ValueError('Gate does not admit live runs')
    if 'input_artifacts' in gate and gate['input_artifacts'] != inputs:
        raise ValueError('Gate grading inputs differ from the selected inputs')
    if 'allowed_schedule' in gate:
        allowed = gate['allowed_schedule']
        if not isinstance(allowed, list) or not allowed:
            raise ValueError('Gate allowed_schedule must be a nonempty list')
        expected = Counter()
        for item in allowed:
            if (not isinstance(item, dict) or set(item) != {'instance_id', 'arm', 'repetitions'}
                    or not isinstance(item['instance_id'], str) or not item['instance_id']
                    or not isinstance(item['arm'], str) or not item['arm']
                    or type(item['repetitions']) is not int or item['repetitions'] <= 0):
                raise ValueError('Gate allowed_schedule entry is malformed')
            key = (item['instance_id'], item['arm'])
            if key in expected:
                raise ValueError('Gate allowed_schedule contains duplicate entries')
            expected[key] = item['repetitions']
        actual = Counter((entry['instance']['instance_id'], entry['arm']) for entry in schedule)
        if actual != expected or len({entry['run_id'] for entry in schedule}) != len(schedule):
            raise ValueError('Gate allowed_schedule differs from requested runs or repetitions')
    roles = gate.get('requested_roles')
    if 'requested_roles' in gate and (not isinstance(roles, dict)
            or not set(roles) <= {'lead', 'production', 'tests', 'cm'}
            or any(not isinstance(value, str) or not value for value in roles.values())):
        raise ValueError('Gate requested_roles is malformed')
    for entry in schedule:
        limits = entry['effective_limits']
        if 'effective_limits' in gate and limits != gate['effective_limits']:
            raise ValueError('Gate effective_limits differ from runtime settings')
        if roles is not None:
            workers = entry.get('worker_models') or []
            actual = {'lead': entry.get('lead_model'),
                      'production': workers[0] if workers else None,
                      'tests': workers[1] if len(workers) > 1 else None,
                      'cm': limits.get('cm_model') if limits.get('cm_enabled') else None}
            if (any(actual[name] != value for name, value in roles.items())
                    or (set(roles) & {'production', 'tests'}
                        and entry.get('condition') not in ('fixed_team', 'hetero_team'))):
                raise ValueError('Gate requested_roles differ from ordered runtime roles')


def prepare(batch, only_instances=None, reverse_arms=False, only_arms=None, wave=None,
            validation_gate_path=None):
    # Selecting a separate gate never promotes or rewrites the default offline gate.
    gate_path = Path(validation_gate_path if validation_gate_path is not None else GATE_PATH).resolve()
    gate = read(gate_path)
    assert gate['status'] == 'PASS' and gate['runtime_sources'] == sources(), 'Validation missing or sources changed'
    _verify_gate_artifacts(gate)
    old = PRIOR / 'pilot_v2'
    stop = read(old / 'USER_STOP.json')
    assert stop['all_started_runs_completed'] and stop['inflight_model_calls_at_stop'] == 0
    started = {read(p)['instance_id'] for p in (old / 'results').glob('*/result.json')}
    public = read(OFFICIAL / 'selected_public.json')
    if wave == 'probepanel':
        assert not only_instances, 'probepanel fixes its own pre-registered instance set'
        selected = [r for r in public if r['instance_id'] not in PROBE_ANCHORS]
        assert len(selected) == 8, 'probe panel expects the eight non-anchor frozen instances'
    elif wave in ('curfew15', 'teamhard16', 'budget15b'):
        # 20260904C waves fix their own instance sets; the schedule below reads
        # the wave table (declared order), not this public-ordered selection.
        assert not only_instances, wave + ' fixes its own pre-registered instance set'
        wave_ids = {instance for instance, *_ in (
            CURFEW15 if wave == 'curfew15' else TEAMHARD16 if wave == 'teamhard16' else BUDGET15B)}
        selected = [r for r in public if r['instance_id'] in wave_ids]
        assert len(selected) == len(wave_ids), wave + ' instance set mismatch with the frozen public selection'
    else:
        selected = [r for r in public if r['instance_id'] not in started][:2]
        if only_instances:
            # Revision-3 batches re-run a named instance on the new adapter; the
            # original task order rule does not apply to them.
            selected = [r for r in public if r['instance_id'] in set(only_instances)]
            assert [r['instance_id'] for r in selected] == list(only_instances), 'Requested instances absent from the frozen public selection'
        else:
            assert [r['instance_id'] for r in selected] == TASK_IDS
    assert all(set(r) <= {'instance_id', 'repo', 'base_commit', 'version', 'problem_statement'} for r in selected)
    versions = read(OFFICIAL / 'versions.json')
    assert sha(OFFICIAL / 'verified.parquet') == versions['dataset_sha256']
    arms = ['solo'] + ['fixed_' + model for model in MODELS]
    lead_model_default = 'gpt-5.6-sol'
    if wave == 'hetero16':
        # NEXT_EXPERIMENT_PLAN §3: solo + fixed anchors + curated hetero teams.
        hetero_pairs = [('gpt-5.6-terra', 'gpt-5.6-luna'), ('gpt-5.6-terra', 'glm-5.3'),
                        ('glm-5.3', 'gpt-5.6-luna'), ('gpt-5.6-terra', 'glm-5.3-flash'), ('gpt-5.6-terra', 'deepseek-v4-flash'),
                        ('gpt-5.6-sol', 'glm-5.3'), ('glm-5.3', 'gpt-5.6-sol')]
        arms = (['solo_' + model for model in MODELS]
                + ['fixed_' + model for model in MODELS]
                + ['hetero_' + a + '__' + b for a, b in hetero_pairs])
    if only_arms:
        arms = [arm for arm in arms if arm in set(only_arms)]
        assert arms, 'No scheduled arm matches --only-arm'
    schedule = []
    effective_call_totals = None
    if wave in REV9_WAVES:
        assert not only_arms and not reverse_arms, 'rev9 waves fix their own arm sequence'
        assert len(selected) == 1, 'rev9 waves run one frozen instance per batch'
        instance = selected[0]
        for arm, rep in REV9_WAVES[wave]:
            entry, override = rev9_entry(arm, lead_model_default)
            if wave == 'budget11':
                override = {**(override or {}), **BUDGET11_OVERRIDE}
            elif wave == 'ablation9' and arm.startswith('hetero_'):
                override = {**(override or {}), **ASSEMBLY9_ON}
            item = {'run_id': instance['instance_id'] + '__' + arm + ('' if rep == 1 else '.rep' + str(rep)),
                    'arm': arm, 'rep': rep, **entry,
                    'instance': instance, 'image': image_name(instance['instance_id'])}
            if override:
                item['limits_override'] = override
            item['effective_limits'] = {**LIMITS, **(override or {})}
            schedule.append(item)
        arms = list(dict.fromkeys(arm for arm, _ in REV9_WAVES[wave]))
        effective_call_totals = {
            'maximum_calls': sum(e['effective_limits']['max_calls'] for e in schedule),
            'sum_token_admission_limits': sum(e['effective_limits']['token_limit'] for e in schedule)}
    elif wave == 'cmscan13':
        assert not only_arms and not reverse_arms, 'cmscan13 fixes its own arm sequence'
        assert len(selected) == 1, 'cmscan13 runs one frozen instance (the anchored hard task)'
        instance = selected[0]
        for arm, base, rep, override in CMSCAN13:
            entry, extra = rev9_entry(base, lead_model_default)
            item = {'run_id': instance['instance_id'] + '__' + arm + ('' if rep == 1 else '.rep' + str(rep)),
                    'arm': arm, 'rep': rep, **entry,
                    'instance': instance, 'image': image_name(instance['instance_id'])}
            merged = {**(extra or {}), **override}
            item['limits_override'] = merged
            item['effective_limits'] = {**LIMITS, **merged}
            schedule.append(item)
        arms = list(dict.fromkeys(arm for arm, *_ in CMSCAN13))
        effective_call_totals = {
            'maximum_calls': sum(e['effective_limits']['max_calls'] for e in schedule),
            'sum_token_admission_limits': sum(e['effective_limits']['token_limit'] for e in schedule)}
    elif wave == 'probepanel':
        assert not only_arms and not reverse_arms, 'probepanel fixes its own arm sequence'
        for instance in selected:
            entry, _extra = rev9_entry(PROBE_ARM, lead_model_default)
            schedule.append({'run_id': instance['instance_id'] + '__' + PROBE_ARM, 'arm': PROBE_ARM,
                'rep': 1, **entry, 'instance': instance,
                'image': image_name(instance['instance_id']), 'effective_limits': {**LIMITS}})
        arms = [PROBE_ARM]
        effective_call_totals = {
            'maximum_calls': sum(e['effective_limits']['max_calls'] for e in schedule),
            'sum_token_admission_limits': sum(e['effective_limits']['token_limit'] for e in schedule)}
    elif wave in ('curfew15', 'teamhard16', 'budget15b'):
        # 20260904C section 4: instance blocks contiguous, interleaving only
        # inside a block; the wave table's declared order IS the schedule.
        assert not only_arms and not reverse_arms, wave + ' fixes its own arm sequence'
        table = CURFEW15 if wave == 'curfew15' else TEAMHARD16 if wave == 'teamhard16' else BUDGET15B
        by_id = {r['instance_id']: r for r in public}
        for instance_id, arm, rep, override in table:
            base = arm.removesuffix('.nocurf').removesuffix('.cm24').removesuffix('.b900')
            entry, extra = rev9_entry(base, lead_model_default)
            item = {'run_id': instance_id + '__' + arm + ('' if rep == 1 else '.rep' + str(rep)),
                    'arm': arm, 'rep': rep, **entry,
                    'instance': by_id[instance_id], 'image': image_name(instance_id)}
            merged = {**(extra or {}), **(override or {})}
            if merged:
                item['limits_override'] = merged
            item['effective_limits'] = {**LIMITS, **merged}
            schedule.append(item)
        arms = list(dict.fromkeys(arm for _, arm, _, _ in table))
        effective_call_totals = {
            'maximum_calls': sum(e['effective_limits']['max_calls'] for e in schedule),
            'sum_token_admission_limits': sum(e['effective_limits']['token_limit'] for e in schedule)}
    else:
        for ordinal, instance in enumerate(selected):
            order = list(reversed(arms)) if (ordinal % 2 == 1) != reverse_arms else arms
            for arm in order:
                if arm.startswith('solo_'):
                    entry = {'condition': 'solo', 'worker_model': None, 'worker_models': [],
                             'lead_model': arm.removeprefix('solo_')}
                elif arm.startswith('hetero_'):
                    pair = arm.removeprefix('hetero_').split('__')
                    entry = {'condition': 'hetero_team', 'worker_model': None,
                             'worker_models': pair, 'lead_model': lead_model_default}
                else:
                    worker_model = None if arm == 'solo' else arm.removeprefix('fixed_')
                    entry = {'condition': 'solo' if arm == 'solo' else 'fixed_team',
                             'worker_model': worker_model,
                             'worker_models': [] if arm == 'solo' else [worker_model] * 2,
                             'lead_model': lead_model_default}
                schedule.append({'run_id': instance['instance_id'] + '__' + arm, 'arm': arm, **entry,
                    'instance': instance, 'image': image_name(instance['instance_id'])})
    manifest = {'created_at': utc(), 'experiment': 'SWE explicit Lead and ordered DERIVE worker roles',
        'selection': {'selected': [{'instance_id': r['instance_id'], 'repo': r['repo']} for r in selected],
            'rule': (PROBE_PANEL_RULE if wave == 'probepanel' else
                     CURFEW15_RULE if wave == 'curfew15' else
                     TEAMHARD16_RULE if wave == 'teamhard16' else
                     BUDGET15B_RULE if wave == 'budget15b' else
                     ('Explicitly requested frozen public instances in original order; selection rationale recorded in validation gate/plan'
                      if only_instances else
                      'First two previously unstarted instances in the original metadata-frozen order; no answer/result-based replacement'))},
        'versions': versions, 'official_directory': str(OFFICIAL),
        'validation_gate_path': str(gate_path), 'validation_gate_sha256': sha(gate_path),
        'runtime_sources': sources(), 'input_artifacts': input_artifacts(),
        'grader_controller': read(OFFICIAL / 'grader_controller.json'),
        'public_instances_sha256': sha(OFFICIAL / 'selected_public.json'),
        'lead_model': next(iter({e['lead_model'] for e in schedule})) if len({e['lead_model'] for e in schedule}) == 1 else None, 'candidate_worker_models': MODELS, 'arms': arms,
        'conditions': sorted({e['condition'] for e in schedule}), 'schedule': schedule,
        'limits': LIMITS,
        'per_entry_limits': ('rev9: each schedule entry carries its merged effective_limits and any limits_override; '
                             'this limits field is the module default only' if effective_call_totals else None),
        'wave': wave, 'scheduled_runs': len(schedule), 'fixed_workers_per_team': 2,
        'worker_roles': ['production implementation', 'independent regression tests'],
        'activation_source': 'experiment_protocol; not a model decision',
        'mechanism_coverage': {'derive': 'fixed_team', 'split': 'not_exposed', 'fission': 'not_exposed',
                               'cm': 'integrated_on_demand'},
        'model_level_policy': 'Equal B admission labels, unranked; not AA or model strength measurements',
        'gpt_effort_requested': 'max', 'gpt_service_tier_requested': 'fast',
        'glm_effort_requested': 'max', 'glm_thinking_requested': True,
        'actual_settings': 'Only provider echoes are observed; absent values remain null',
        'maximum_calls': (effective_call_totals or {}).get(
            'maximum_calls', len(schedule) * LIMITS['max_calls']),
        'sum_token_admission_limits': (effective_call_totals or {}).get(
            'sum_token_admission_limits', len(schedule) * LIMITS['token_limit']),
        'max_parallel_runs': 2, 'batch_wall_limit_seconds': 10800,
        'grading': 'Unmodified official v4.1.0 run_instance after all candidate work ends and patch freezes',
        'prior_batch': {'path': str(old), 'manifest_sha256': sha(old / 'manifest.json'),
            'stop_sha256': sha(old / 'USER_STOP.json'), 'calls': stop['completed_calls'], 'tokens': stop['candidate_tokens']},
        'scope': 'Explicit role experiment with optional CM; fixed DERIVE does not measure autonomous activation. SPLIT/FISSION/DSH bridge are not exercised',
        'comparison': 'Selected exposed tasks with declared role and budget settings; not a model ranking or SOTA claim',
        'stop_policy': 'After each pair: infrastructure/grader failure, unknown/pending usage or absent required worker calls stops new pairs. Known empty patch and ordinary unresolved continue. STOP_AFTER_PAIR supports user-directed boundary stop.',
        'cost_usd': None, 'cost_note': 'No verified monetary charge source; record raw usage and timing'}
    for entry in schedule:
        effective = resolve_limits(LIMITS, entry.get('limits_override'))
        if entry.get('effective_limits') is not None and entry['effective_limits'] != effective:
            raise ValueError('Schedule effective_limits disagrees with runtime')
        entry['effective_limits'] = effective
        entry['configuration_sha256'] = hashlib.sha256(json.dumps(
            {k: entry.get(k) for k in ('condition', 'lead_model', 'worker_model', 'worker_models', 'effective_limits')},
            sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        entry['grader_contract'] = {'input_artifacts': manifest['input_artifacts'],
                                    'environment_sha': manifest['runtime_sources'][ENV_SOURCE]}
    _validate_runtime_entries(manifest)
    _validate_gate_scope(gate, schedule, manifest['input_artifacts'])
    batch.mkdir(parents=True, exist_ok=True)
    target = batch / 'manifest.json'
    if target.exists():
        previous = read(target)
        assert {k:v for k,v in previous.items() if k != 'created_at'} == {k:v for k,v in manifest.items() if k != 'created_at'}, 'Frozen batch differs'
        return previous
    dump(target, manifest)
    for relative, expected in manifest['runtime_sources'].items():
        dest = batch / 'source_snapshot' / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / relative, dest)
        assert sha(dest) == expected
    for relative, expected in manifest['input_artifacts'].items():
        dest = batch / 'inputs_snapshot' / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(OFFICIAL / relative, dest)
        assert sha(dest) == expected
    snapshot = batch / 'validation_snapshot'
    snapshot.mkdir()
    shutil.copyfile(gate_path, snapshot / 'gate.json')
    for relative, expected in gate.get('validation_artifacts', {}).items():
        dest = snapshot / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(HERE / 'validation' / relative, dest)
        assert sha(dest) == expected
    report(batch)
    return manifest

def verify(batch):
    manifest = read(batch / 'manifest.json')
    assert sha(Path(manifest['validation_gate_path'])) == manifest['validation_gate_sha256'], 'Validation drift'
    assert sources() == manifest['runtime_sources'], 'Runtime drift; use a separate revision'
    assert input_artifacts() == manifest['input_artifacts'], 'Grader/input drift'
    gate = read(Path(manifest['validation_gate_path']))
    if gate.get('status') != 'PASS' or gate.get('runtime_sources') != manifest['runtime_sources']:
        raise ValueError('Gate no longer admits this source snapshot')
    _verify_gate_artifacts(gate)
    _verify_gate_artifacts(gate, batch / 'validation_snapshot')
    _validate_runtime_entries(manifest)
    _validate_gate_scope(gate, manifest['schedule'], manifest['input_artifacts'])
    return manifest

def instance_blocks(schedule):
    """Group consecutive schedule entries by instance id, preserving order."""
    blocks = []
    for entry in schedule:
        key = entry['instance']['instance_id']
        if not blocks or blocks[-1][0] != key:
            blocks.append((key, []))
        blocks[-1][1].append(entry)
    return blocks


def result_stop(result):
    """Apply the predeclared completion gate equally to both team conditions."""
    score, budget = result.get('score') or {}, result['budget']
    health = result.get('execution_health') or {}
    known_empty = score.get('failure_kind') == 'candidate_empty_patch' and score.get('resolved') is False
    no_workers = result['condition'] in ('fixed_team', 'hetero_team') and result.get('workers_with_actual_calls') != 2
    if (result.get('infrastructure_error') or health.get('status') == 'host_error'
            or (not score.get('completed') and not known_empty)
            or budget['unknown_call_count'] or budget['pending_call_count'] or no_workers):
        return {'reason': 'predeclared_gate', 'run_id': result['run_id'],
                'infrastructure_error': result.get('infrastructure_error'), 'execution_health': health,
                'score': score, 'unknown_calls': budget['unknown_call_count'],
                'pending_calls': budget['pending_call_count'], 'worker_call_coverage_missing': no_workers}
    return None


def run(batch):
    manifest = verify(batch)
    invocation = uuid4().hex
    started, clock = utc(), time.monotonic()
    stop = None
    dump(batch / 'invocations' / (invocation + '.started.json'), {'started_at': started, 'manifest_sha256': sha(batch / 'manifest.json')})
    previous = read(batch / 'batch.json') if (batch / 'batch.json').exists() else {}
    cumulative_before = previous.get('cumulative_invocation_wall_seconds', 0)
    schedule = manifest['schedule']
    # rev10: single-instance waves keep the historical six-entry blocks;
    # probe panels give each instance its own block so per-instance image
    # lifecycle is unchanged.
    for instance_id, entries in instance_blocks(schedule):
        pending_any = any(not (batch / 'results' / e['run_id'] / 'result.json').exists() for e in entries)
        if not pending_any:
            continue
        if (batch / 'STOP_AFTER_PAIR').exists():
            stop = {'reason': 'user_boundary_stop', 'control_sha256': sha(batch / 'STOP_AFTER_PAIR')}
            break
        verify(batch)
        try:
            dump(batch / 'images' / (instance_id + '.json'), ensure_image(instance_id))
        except Exception as exc:
            stop = {'reason': 'image_error', 'instance_id': instance_id, 'error': str(exc)}
            break
        for start in range(0, len(entries), 2):
            verify(batch)
            if (batch / 'STOP_AFTER_PAIR').exists():
                stop = {'reason': 'user_boundary_stop', 'control_sha256': sha(batch / 'STOP_AFTER_PAIR')}
                break
            if cumulative_before + time.monotonic() - clock >= manifest['batch_wall_limit_seconds']:
                stop = {'reason': 'batch_admission_deadline'}
                break
            pending = []
            for entry in entries[start:start+2]:
                directory = batch / 'results' / entry['run_id']
                if (directory / 'result.json').exists():
                    continue
                if directory.exists():
                    stop = {'reason': 'unfinished_run_requires_reconciliation', 'run_id': entry['run_id']}
                    break
                pending.append(entry)
            if stop:
                break
            if not pending:
                continue
            print(json.dumps({'event': 'pair_started', 'at': utc(), 'instance_id': instance_id, 'run_ids': [e['run_id'] for e in pending]}), flush=True)
            results = []
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = {}
                for entry in pending:
                    try:
                        futures[executor.submit(SweRun(batch, entry).run)] = entry
                    except Exception as exc:
                        stop = {'reason': 'runner_initialization_error', 'run_id': entry['run_id'], 'error': str(exc)}
                        break
                for future in as_completed(futures):
                    entry = futures[future]
                    try:
                        result = future.result()
                        results.append(result)
                        report(batch)
                        print(json.dumps({'event': 'run_completed', 'run_id': result['run_id'],
                            'arm': entry['arm'], 'resolved': (result.get('score') or {}).get('resolved'),
                            'calls': result['call_count'], 'tokens': result['budget']['total_tokens'],
                            'actual_workers': result.get('workers_with_actual_calls'), 'team_execution_valid': result.get('team_execution_valid'),
                            'wall_seconds': result['wall_seconds']}), flush=True)
                    except Exception as exc:
                        stop = {'reason': 'uncaught_runtime_error', 'run_id': entry['run_id'], 'error': type(exc).__name__ + ': ' + str(exc)}
            for result in results:
                stop = stop or result_stop(result)
            if stop:
                break
        try:
            dump(batch / 'cleanup' / (instance_id + '.' + invocation + '.json'), cleanup_image(instance_id))
        except Exception as exc:
            stop = stop or {'reason': 'image_cleanup_error', 'instance_id': instance_id, 'error': str(exc)}
        if stop:
            break
    summary = report(batch)
    record = {'invocation_id': invocation, 'started_at': started, 'completed_at': utc(),
        'wall_seconds': time.monotonic() - clock,
        'cumulative_invocation_wall_seconds': cumulative_before + time.monotonic() - clock,
        'stop': stop, 'completed_runs': summary['completed_runs'], 'scheduled_runs': len(schedule)}
    dump(batch / 'invocations' / (invocation + '.finished.json'), record)
    dump(batch / 'batch.json', record)
    print(json.dumps({'event': 'batch_finished', 'completed': summary['completed_runs'], 'stop': stop}), flush=True)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['prepare', 'run', 'report'])
    parser.add_argument('--batch', type=Path, default=HERE / 'pilot_v2')
    parser.add_argument('--gate', type=Path, default=None,
                        help='prepare only: separately validated gate; PASS and current source hashes are required')
    parser.add_argument('--only-instance', action='append', default=None,
                        help='Revision-3 rerun: restrict the batch to the named instance(s)')
    parser.add_argument('--only-arm', action='append', default=None,
                        help='Restrict the batch to the named arm(s), e.g. solo or fixed_gpt-5.6-sol')
    parser.add_argument('--wave', default=None,
                        help='Named experiment wave, e.g. hetero16 (NEXT_EXPERIMENT_PLAN section 3)')
    parser.add_argument('--reverse-arms', action='store_true',
                        help='Use the reversed arm order for every selected instance')
    args = parser.parse_args()
    if args.gate is not None and args.command != 'prepare':
        parser.error('--gate is only valid for prepare; run uses the frozen manifest gate')
    batch = args.batch.resolve()
    if args.command == 'prepare':
        value = prepare(batch, only_instances=args.only_instance, reverse_arms=args.reverse_arms,
                        only_arms=args.only_arm, wave=args.wave, validation_gate_path=args.gate)
        print(json.dumps({'batch': str(batch), 'runs': len(value['schedule']), 'manifest_sha256': sha(batch / 'manifest.json')}))
    elif args.command == 'run':
        run(batch)
    else:
        value = report(batch)
        print(json.dumps({'completed': value['completed_runs'], 'scheduled': value['scheduled_runs']}))

if __name__ == '__main__':
    main()
