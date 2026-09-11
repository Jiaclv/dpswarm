"""Memory-profile installation only; no model, Docker or qualification runs."""
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('tested_memory_profile',ROOT/'memory_profile.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


@pytest.fixture
def rig(tmp_path):
    names=['environment','runner','r2','selector','cli','data','prepare_a2','known_aliases']
    modules={}
    for name in names:
        module=ModuleType(m.PREFIX+'.'+name)
        path=tmp_path/(name+'.py');path.write_text('# fake runtime')
        module.__file__=str(path);modules[name]=module
    class Env:
        def __init__(self,instance,run_dir,**kwargs):
            self.instance,self.run_dir=instance,Path(run_dir)
            self.memory=kwargs.get('memory','3g')
        def fork(self,path):return type(self)(self.instance,path,memory=self.memory)
        clone=fork
    class Run:
        def __init__(self,*args,**kwargs):self.factory=kwargs.get('environment_factory')
    class R2:
        def __init__(self,*args,**kwargs):self.factory=kwargs.get('environment_factory')
    class Selector:
        def __init__(self,*args,**kwargs):self.factory=kwargs.get('environment_factory')
    calls=[]
    def grade(instance,patch,expected,run_dir,**kwargs):
        env=modules['environment'].ValueEnvironment(instance,run_dir,memory=kwargs.get('memory','3g'))
        calls.append(('fake_grade',env.memory,kwargs.get('memory')))
        return {'memory':env.memory}
    def one(instance,**kwargs):
        calls.append('fake_qualification')
        directory=Path(kwargs['owned_root'])/instance['instance_id']
        if not (directory/'qualification.json').exists():m._write(directory/'qualification.json',{'qualified':True})
        return {'qualified':True}
    def a1(official,*,attempt):calls.append('fake_a1');return []
    modules['environment'].ValueEnvironment=Env
    modules['environment'].rootgrade_terminal=modules['environment'].grade_terminal=grade
    modules['runner'].ValueRun=Run;modules['r2'].R2Run=R2;modules['selector'].RestrictedSelector=Selector
    modules['prepare_a2']._qualify_one=one
    modules['data'].preflight_a1=a1;modules['data'].OFFICIAL=tmp_path/'official'
    modules['data'].load_public=lambda *args:[{'instance_id':'task'}]
    modules['known_aliases'].early_env=Env;modules['known_aliases'].early_grade=grade
    modules['known_aliases'].early_one=one
    profile=m._read(ROOT/'resource-defaults.json')
    def install():
        config,digest,source=m._profile(profile)
        return m._install(config,digest,source,tmp_path/'receipt.json',modules)
    return tmp_path,modules,calls,profile,install


def test_install_starts_nothing_and_records_profile_and_bindings(rig):
    path,modules,calls,profile,install=rig
    receipt=install()
    assert receipt['status']=='installed' and receipt['calls_started']==receipt['containers_started']==0
    assert calls==[]
    assert '768m' in receipt['auxiliary_limits']
    assert any('early_grade' in name for name in receipt['patched_bindings'])
    assert install()==receipt


def test_explicit_old_memory_and_captured_class_alias_are_forced_at_creation(rig):
    path,modules,calls,profile,install=rig
    early=modules['environment'].ValueEnvironment
    install()
    env=early({},path/'lead',memory='3g')
    assert env.memory=='1g'
    assert env.fork(path/'worker').memory=='1g'
    assert env.clone(path/'clone').memory=='1g'
    assert m._read(path/'lead'/'memory-profile.json')['limits_apply_at_creation']


def test_all_solver_and_selector_factories_override_preimported_class_aliases(rig):
    path,modules,calls,profile,install=rig
    cached=[modules['runner'].ValueRun,modules['r2'].R2Run,modules['selector'].RestrictedSelector]
    install()
    for cls in cached:
        obj=cls(environment_factory=object)
        assert obj.factory is modules['environment'].ValueEnvironment


def test_known_grader_alias_gets_explicit_memory_and_local_old_alias_still_forces_env(rig):
    path,modules,calls,profile,install=rig
    early_local=modules['environment'].rootgrade_terminal
    install()
    assert modules['known_aliases'].early_grade({},None,None,path/'grader',memory='3g')['memory']=='1g'
    assert calls[-1]==('fake_grade','1g','1g')
    assert early_local({},None,None,path/'old-local')['memory']=='1g'


@pytest.mark.parametrize('existing',[{'memory':'3g'},{'memory':'1g'}])
def test_unbound_existing_grader_is_refused_without_any_grade(rig,existing):
    path,modules,calls,profile,install=rig
    m._write(path/'old'/'grader'/'request.json',existing)
    install()
    with pytest.raises(m.MemoryProfileError,match='no 1-GiB profile binding'):
        modules['environment'].rootgrade_terminal({},None,None,path/'old')
    assert calls==[]


def test_old_qualification_cache_refused_without_rerun_including_imported_alias(rig):
    path,modules,calls,profile,install=rig
    m._write(path/'owned'/'task'/'qualification.json',{'qualified':True})
    install()
    with pytest.raises(m.MemoryProfileError,match='new attempt explicitly'):
        modules['known_aliases'].early_one({'instance_id':'task'},owned_root=path/'owned')
    assert calls==[]


def test_new_qualification_receipt_hash_prevents_later_cache_tampering(rig):
    path,modules,calls,profile,install=rig
    install()
    qualify=modules['prepare_a2']._qualify_one
    qualify({'instance_id':'task'},owned_root=path/'owned')
    proof=path/'owned'/'task'/'memory-profile-qualification.json'
    assert m._read(proof)['qualified'] is True
    qualify({'instance_id':'task'},owned_root=path/'owned')
    m._write(path/'owned'/'task'/'qualification.json',{'qualified':False})
    with pytest.raises(m.MemoryProfileError,match='new attempt explicitly'):
        qualify({'instance_id':'task'},owned_root=path/'owned')
    assert len(calls)==2


def test_a1_old_cache_rejected_before_preflight(rig):
    path,modules,calls,profile,install=rig
    official=path/'official';m._write(official/'grader'/'preflight'/'task'/'attempt_03'/'qualification.json',{'qualified':True})
    install()
    with pytest.raises(m.MemoryProfileError,match='new attempt explicitly'):
        modules['data'].preflight_a1(official)
    assert calls==[]


def test_different_profile_cannot_be_layered_or_receipt_silently_reused(rig):
    path,modules,calls,profile,install=rig
    install()
    config,digest,source=m._profile(profile|{'profile_id':'different'})
    with pytest.raises(m.MemoryProfileError,match='different memory profile'):
        m._install(config,digest,source,path/'receipt.json',modules)


def test_nonreviewed_limits_rejected(rig):
    *_,profile,install=rig
    with pytest.raises(m.MemoryProfileError,match='reviewed'):
        m._profile(profile|{'candidate_memory':'3g'})
