"""Install the explicit 1-GiB creation profile in a fresh independent process.

After admitting/importing the intended runtime (the batch snapshot for solver
execution, the preparation checkout for qualification), load this operator file
by importlib.util.spec_from_file_location, then call install(profile, receipt).
No model, container, qualification or pipeline is started by installation.

The original files and frozen hashes remain unchanged. Process-local class
constructors and known aliases are covered, including aliases imported before
installation. Old 3-GiB qualification caches are refused, never auto-rerun.
"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import wraps
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import threading
from uuid import uuid4

HERE = Path(__file__).resolve().parent
PREFIX = 'modelbench.minimal_value_20260905'
DEFAULT_PROFILE = HERE / 'resource-defaults.json'


class MemoryProfileError(RuntimeError):
    pass


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=True, sort_keys=True, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _profile(value):
    if isinstance(value, (str, Path)):
        path = Path(value).resolve()
        config, digest = _read(path), _sha(path)
        source = str(path)
    elif isinstance(value, dict):
        config = json.loads(json.dumps(value))
        digest = hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=True,
                    separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
        source = 'in_memory_canonical_json'
    else:
        raise MemoryProfileError('A profile file or mapping is required')
    expected = {'version': 1, 'candidate_memory': '1g', 'evaluation_memory': '1g',
                'auxiliary_memory_ceiling': '1g', 'preserve_lower_auxiliary_limits': True,
                'existing_controller_and_parser_memory': '768m', 'memory_swap_equals_memory': True}
    if any(config.get(key) != wanted for key, wanted in expected.items()):
        raise MemoryProfileError('Only the reviewed 1-GiB creation profile is supported')
    return config, digest, source


def _modules():
    names = ['environment', 'runner', 'r2', 'selector', 'cli', 'data']
    found = {name: importlib.import_module(PREFIX + '.' + name) for name in names}
    # Operations are deliberately absent in solver snapshots. In the original
    # qualification subprocess, import this known entry before applying aliases.
    try:
        found['prepare_a2'] = importlib.import_module(PREFIX + '.operations.prepare_a2')
    except ModuleNotFoundError as exc:
        if exc.name not in (PREFIX + '.operations', PREFIX + '.operations.prepare_a2'):
            raise
    root = Path(found['environment'].__file__).resolve().parents[2]
    for name, module in found.items():
        if not Path(module.__file__).resolve().is_relative_to(root):
            raise MemoryProfileError('Memory profile refuses mixed runtime roots: ' + name)
    return found


def _install(config, digest, source, receipt_path, modules):
    receipt_path = Path(receipt_path).resolve()
    environment = modules['environment']
    prior = getattr(environment, '_memory_profile_installation', None)
    if prior is not None:
        if prior['profile_sha256'] != digest or prior['receipt_path'] != str(receipt_path):
            raise MemoryProfileError('A different memory profile or receipt is already installed in this process')
        return _read(receipt_path)
    if receipt_path.exists():
        raise MemoryProfileError('Installation receipt already exists; use a fresh process receipt')
    event_path = receipt_path.with_name(receipt_path.stem + '.events.jsonl')
    event_lock = threading.RLock()
    patched = []
    profile_binding = {'profile_sha256': digest, 'profile_id': config.get('profile_id'),
                       'receipt_path': str(receipt_path), 'candidate_memory': '1g', 'evaluation_memory': '1g',
                       'limits_apply_at_creation': True}

    def event(kind, **payload):
        with event_lock:
            with event_path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps({'at': _utc(), 'event': kind, 'profile_sha256': digest, **payload},
                                        ensure_ascii=True, allow_nan=False) + '\n')
                stream.flush()
                os.fsync(stream.fileno())

    def bind_environment(run_dir):
        directory = Path(run_dir).resolve()
        binding_path = directory / 'memory-profile.json'
        if binding_path.exists():
            if _read(binding_path).get('profile_sha256') != digest:
                raise MemoryProfileError('Existing environment uses a different memory profile')
        else:
            for existing in (directory / 'container.json', directory / 'grader' / 'request.json'):
                if existing.exists():
                    raise MemoryProfileError('Pre-existing environment/grader has no 1-GiB profile binding; choose a new attempt')
            _write(binding_path, profile_binding)
        request_path = directory / 'grader' / 'request.json'
        if request_path.exists() and _read(request_path).get('memory') != '1g':
            raise MemoryProfileError('Existing grader request is not 1 GiB; choose a new attempt')

    def cached_qualification(directory):
        directory = Path(directory)
        summary = directory / 'qualification.json'
        if not summary.exists():
            return
        binding = directory / 'memory-profile-qualification.json'
        if (not binding.exists() or _read(binding).get('profile_sha256') != digest
                or _read(binding).get('qualification_sha256') != _sha(summary)):
            raise MemoryProfileError('Existing qualification is not verified under this 1-GiB profile; choose a new attempt explicitly')

    def qualify_receipt(directory):
        directory = Path(directory)
        summary = directory / 'qualification.json'
        if summary.exists():
            value = profile_binding | {'qualification_sha256': _sha(summary),
                    'at': _utc(), 'qualified': _read(summary).get('qualified')}
            target = directory / 'memory-profile-qualification.json'
            if target.exists():
                old = _read(target)
                if old.get('profile_sha256') != digest or old.get('qualification_sha256') != value['qualification_sha256']:
                    raise MemoryProfileError('Qualification profile evidence cannot be replaced')
            else:
                _write(target, value)
            event('qualification_profile_bound', directory=str(directory), qualification_sha256=value['qualification_sha256'])

    receipt = {'status': 'installing', 'at': _utc(), 'pid': os.getpid(),
               'profile': config, 'profile_sha256': digest, 'profile_source': source,
               'installer_sha256': _sha(__file__), 'event_path': str(event_path),
               'runtime_modules': {name: {'path': str(Path(module.__file__).resolve()),
                                          'sha256': _sha(module.__file__)} for name, module in modules.items()},
               'scope': 'Fresh-process creation override; source files unchanged; no workload started',
               'auxiliary_limits': 'Existing 768m controller/parser limits preserved below 1g',
               'existing_trial': 'No effect on the already-running trial; dynamic changes require separate evidence'}
    _write(receipt_path, receipt)
    try:
        env_class = environment.ValueEnvironment
        original_env_init = env_class.__init__

        @wraps(original_env_init)
        def environment_init(self, instance, run_dir, **kwargs):
            requested = kwargs.get('memory', 'inherited_default')
            bind_environment(run_dir)
            kwargs['memory'] = config['candidate_memory']
            original_env_init(self, instance, run_dir, **kwargs)
            if self.memory != '1g':
                raise MemoryProfileError('Environment constructor did not retain the 1-GiB limit')
            event('environment_constructed', run_dir=str(Path(run_dir).resolve()),
                  requested_memory=requested, effective_memory=self.memory)

        env_class.__init__ = environment_init
        patched.append(environment.__name__ + '.ValueEnvironment.__init__ (in place; prior class aliases retained)')

        # In-place constructor replacement also covers class aliases that were
        # captured before installation and R2's selector_factory default object.
        for module_name, class_name in (('runner', 'ValueRun'), ('r2', 'R2Run'), ('selector', 'RestrictedSelector')):
            cls = getattr(modules[module_name], class_name)
            original = cls.__init__

            def force_factory(original):
                @wraps(original)
                def wrapped(self, *args, **kwargs):
                    kwargs['environment_factory'] = env_class
                    return original(self, *args, **kwargs)
                return wrapped

            cls.__init__ = force_factory(original)
            patched.append(modules[module_name].__name__ + '.' + class_name + '.__init__')

        replacements = []
        original_grade = environment.rootgrade_terminal

        @wraps(original_grade)
        def grade(*args, **kwargs):
            kwargs['memory'] = config['evaluation_memory']
            event('grading_requested', effective_memory='1g')
            return original_grade(*args, **kwargs)

        replacements.append((original_grade, grade))

        if 'prepare_a2' in modules:
            module = modules['prepare_a2']
            original_one = module._qualify_one

            @wraps(original_one)
            def qualify_one(instance, **kwargs):
                directory = Path(kwargs['owned_root']) / instance['instance_id']
                cached_qualification(directory)
                result = original_one(instance, **kwargs)
                qualify_receipt(directory)
                return result

            replacements.append((original_one, qualify_one))
        if hasattr(modules['data'], 'preflight_a1'):
            original_a1 = modules['data'].preflight_a1

            @wraps(original_a1)
            def preflight_a1(official=modules['data'].OFFICIAL, *, attempt='attempt_03'):
                official = Path(official)
                public = modules['data'].load_public('a1', official)
                directories = [official / 'grader' / 'preflight' / row['instance_id'] / attempt for row in public]
                for directory in directories:
                    cached_qualification(directory)
                result = original_a1(official, attempt=attempt)
                for directory in directories:
                    qualify_receipt(directory)
                return result

            replacements.append((original_a1, preflight_a1))

        # Replace functions in all loaded modules of this known experiment and
        # __main__. Do not inspect closures, arbitrary external modules or secrets.
        alias_modules = {id(module): module for module in modules.values()}
        for name, module in list(sys.modules.items()):
            if module is not None and (name == '__main__' or name == PREFIX or name.startswith(PREFIX + '.')):
                alias_modules[id(module)] = module
        for module in alias_modules.values():
            for name, value in list(vars(module).items()):
                for original, replacement in replacements:
                    if value is original:
                        setattr(module, name, replacement)
                        patched.append(module.__name__ + '.' + name)
                        break
        receipt.update(status='installed', installed_at=_utc(), patched_bindings=sorted(set(patched)),
                       qualification_cache_policy='Refuse unbound or changed cache; no automatic rerun',
                       calls_started=0, containers_started=0)
        _write(receipt_path, receipt)
        environment._memory_profile_installation = {'profile_sha256': digest, 'receipt_path': str(receipt_path)}
        return receipt
    except BaseException as exc:
        receipt.update(status='installation_failed_abort_process', error_type=type(exc).__name__, patched_bindings=patched)
        _write(receipt_path, receipt)
        raise


def install(profile=DEFAULT_PROFILE, receipt_path=None):
    """Apply process-local creation defaults, returning a durable install receipt.

    Call only in a fresh independent process before constructing any run. This
    API does not update live containers, run qualification, or choose new attempts.
    The receipt and per-environment/qualification sidecars must accompany the new
    resource variant when interpreting results; a source hash alone is not proof
    that a past run used this profile.
    """
    if receipt_path is None:
        raise MemoryProfileError('An explicit fresh installation receipt path is required')
    config, digest, source = _profile(profile)
    return _install(config, digest, source, receipt_path, _modules())
