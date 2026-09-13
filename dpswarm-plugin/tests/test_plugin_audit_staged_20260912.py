"""Audit vocabulary parity and payload checks for real native staged emitters."""
import copy
import re
from pathlib import Path

import pytest

from dpswarm.plugin_audit import EVENT_TYPES, PluginAuditError, PluginAuditStore, validate_transaction


def transaction(events, revision=0, identity='native-txn'):
    return {'root_session_id': 'root', 'expected_revision': revision, 'transaction_id': identity, 'events': events}


def event(kind, data):
    return {'type': 'dpswarm/' + kind, 'data': data}


def emitted_events():
    common = {'version': 1, 'root_session_id': 'root', 'run_id': 'run-1', 'message_id': 'm-1',
              'from': 'upstream', 'to': 'downstream', 'kind': 'fact', 'at': 1780000000000}
    return [
        event('handoff-profile', {'root_session_id': 'root', 'artifact_id': 'downstream', 'phase': 'second',
            'profile': 'semantic', 'decider': 'rule', 'upstreams': ['upstream']}),
        event('handoff-profile', {'root_session_id': 'root', 'artifact_id': 'downstream', 'phase': None,
            'profile': 'verbatim', 'decider': 'rule', 'upstreams': ['upstream'], 'via': 'wake'}),
        event('write-scope', {'root_session_id': 'root', 'worker_session_id': 'worker', 'subtask': 'upstream',
            'scopes': ['src/**', 'upstream.html'], 'run_id': None, 'claimed_at': 1780000000000, 'state': 'claimed', 'version': 1}),
        event('artifact-ready-manifest', {'artifact_id': 'upstream', 'manifest_digest': 'a' * 64,
            'candidate_files': [{'path': 'upstream.html', 'operation': 'file', 'sha256': 'b' * 64, 'size': 12},
                                {'path': 'removed.css', 'operation': 'deleted', 'sha256': None, 'size': 0}],
            'view_path': 'K:/workspace/snapshots/abc/view'}),
        event('artifact-consumed-manifest', {'worker_session_id': 'worker', 'artifact_id': 'upstream',
            'manifest_digest': 'a' * 64, 'paths': ['upstream.html']}),
        event('mailbox-queued', {**common, 'delivery': 'inject', 'refs': ['upstream'], 'ts': 1780000000000, 'pending_for_target': 1}),
        event('mailbox-delivered', {**common, 'via': 'inject', 'carrier': 'live-channel'}),
        event('mailbox-delivered', {**common, 'via': 'acknowledge', 'carrier': 'lead-read'}),
        event('mailbox-rejected', {**common, 'run_id': None, 'message_id': None, 'from': None, 'to': None,
            'kind': 'approve', 'code': 'DPSWARM_MAILBOX_MESSAGE_INVALID', 'detail': 'Control intent is rejected, not queued.'}),
    ]


def test_actual_staged_handoff_scope_and_mailbox_events_persist_and_replay(tmp_path):
    directory = tmp_path / 'audit'
    store = PluginAuditStore(directory, 'root', create=True)
    body = transaction(emitted_events())
    try:
        result = store.append(body)
        assert result['revision'] == 1
        assert [row['type'] for row in result['events']] == [row['type'] for row in body['events']]
        assert store.append(body)['idempotent']
        saved = store.read()
    finally:
        store.close()
    restored = PluginAuditStore(directory, 'root')
    try:
        assert restored.read() == saved
    finally:
        restored.close()


@pytest.mark.parametrize('case', ['bad_digest', 'traversal', 'case_duplicate', 'embedded_bytes',
    'relative_view', 'bad_size', 'deleted_with_hash', 'file_directory_overlap',
    'absolute_consumed', 'missing_worker', 'consumed_duplicate', 'wrong_root',
    'model_handoff', 'unknown_profile', 'invalid_phase', 'claim_version',
    'queued_control', 'wrong_delivery'])
def test_malformed_native_audit_events_reject_before_commit(tmp_path, case):
    rows = emitted_events()
    if case == 'bad_digest':
        selected = rows[3]; selected['data']['manifest_digest'] = 'not-a-hash'
    elif case == 'traversal':
        selected = rows[3]; selected['data']['candidate_files'][0]['path'] = '../escape.html'
    elif case == 'case_duplicate':
        selected = rows[3]; selected['data']['candidate_files'].append({**selected['data']['candidate_files'][0], 'path': 'UPSTREAM.HTML'})
    elif case == 'embedded_bytes':
        selected = rows[3]; selected['data']['candidate_files'][0]['content_base64'] = 'YQ=='
    elif case == 'relative_view':
        selected = rows[3]; selected['data']['view_path'] = 'snapshots/view'
    elif case == 'bad_size':
        selected = rows[3]; selected['data']['candidate_files'][0]['size'] = True
    elif case == 'deleted_with_hash':
        selected = rows[3]; selected['data']['candidate_files'][1]['sha256'] = 'a' * 64
    elif case == 'file_directory_overlap':
        selected = rows[3]; selected['data']['candidate_files'].append({**selected['data']['candidate_files'][0], 'path': 'upstream.html/nested.js'})
    elif case == 'absolute_consumed':
        selected = rows[4]; selected['data']['paths'] = ['K:/elsewhere/input.js']
    elif case == 'missing_worker':
        selected = rows[4]; del selected['data']['worker_session_id']
    elif case == 'consumed_duplicate':
        selected = rows[4]; selected['data']['paths'].append('UPSTREAM.HTML')
    elif case == 'wrong_root':
        selected = rows[3]; selected['data']['root_session_id'] = 'foreign'
    elif case == 'model_handoff':
        selected = rows[0]; selected['data']['decider'] = 'model'
    elif case == 'unknown_profile':
        selected = rows[0]; selected['data']['profile'] = 'guess'
    elif case == 'invalid_phase':
        selected = rows[0]; selected['data']['phase'] = []
    elif case == 'claim_version':
        selected = rows[2]; selected['data']['version'] = True
    elif case == 'queued_control':
        selected = rows[5]; selected['data']['kind'] = 'approve'
    else:
        selected = rows[5]; selected['data']['delivery'] = 'followup'
    store = PluginAuditStore(tmp_path / 'audit', 'root', create=True)
    try:
        before = store.path.read_bytes()
        with pytest.raises(PluginAuditError):
            store.append(transaction([selected]))
        assert store.path.read_bytes() == before
        assert store.read()['revision'] == 0
    finally:
        store.close()


def test_manifest_audit_accepts_posix_views_and_content_addressed_refs():
    row = emitted_events()[3]
    row['data']['view_path'] = '/workspace/snapshots/abc/view'
    file = row['data']['candidate_files'][0]
    file['blob_ref'] = file['sha256']
    assert validate_transaction(transaction([row]), 'root')['events'] == [row]


def test_actual_js_audit_emitter_vocabulary_remains_supported():
    """Catch protocol drift using emitter sources rather than a copied event list."""
    library = Path(__file__).resolve().parents[2] / 'dpswarm-dsh-plugin' / 'lib'
    direct = re.compile(r"journal\.append\([^,]+,\s*['\"](dpswarm/[a-z-]+)['\"]")
    emitted = set()
    for path in library.glob('*.js'):
        emitted.update(direct.findall(path.read_text(encoding='utf-8')))
    for filename, prefix in [('budget-runtime.js', 'dpswarm/worker-budget-'), ('cm-runtime.js', 'dpswarm/cm-')]:
        source = (library / filename).read_text(encoding='utf-8')
        suffixes = set(re.findall(r"eventName\(['\"]([a-z-]+)['\"]\)", source))
        suffixes.update(re.findall(r"this\.append\([^,]+,\s*['\"]([a-z-]+)['\"]", source))
        emitted.update(prefix + suffix for suffix in suffixes)
    for filename in ('mailbox.js', 'team-required.js', 'lead-route.js'):
        source = (library / filename).read_text(encoding='utf-8')
        emitted.update(re.findall(r"(?:=|:)\s*['\"](dpswarm/(?:mailbox-[a-z-]+|team-required-[a-z-]+|route-bound))['\"]", source))
    assert 'dpswarm/handoff-profile' in emitted
    assert 'dpswarm/artifact-ready-manifest' in emitted
    assert 'dpswarm/artifact-consumed-manifest' in emitted
    assert not emitted - EVENT_TYPES, f'Native emitters missing from Python vocabulary: {sorted(emitted - EVENT_TYPES)}'
