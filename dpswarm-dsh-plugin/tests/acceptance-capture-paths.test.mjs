import assert from 'node:assert/strict'
import test from 'node:test'
import { mkdtemp, mkdir, readFile, symlink, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, parse, relative } from 'node:path'
import { AcceptanceRuntime } from '../lib/acceptance-runtime.js'
import { verifyCandidateSnapshot } from '../lib/candidate-snapshot.js'

// Same provenance fields as ec26c449's durable native write/edit diagnostics.
const observation = (path, operation = 'write', result_seq = 23, extra = {}) => ({
  path, operation, tool_name: operation, call_id: 'observed-call-' + result_seq,
  call_seq: result_seq - 1, result_seq, at: 1789182335259,
  source: 'native-successful-tool', path_kind: 'tool-argument; resolved target not re-opened',
  verification: 'unverified', current_file_state: 'unknown', ...extra,
})
const delivery = candidates => ({ role: 'implementer', item_id: 'implementation', worker_session_id: 'native-child',
  diagnostic: { closeout: { candidates } } })
async function fixture({ entries = ['index.html'], html = '<p>saved self-contained candidate</p>' } = {}) {
  const base = await mkdtemp(join(tmpdir(), 'dpswarm-capture-paths-')), cwd = join(base, 'workspace')
  await mkdir(cwd); await writeFile(join(cwd, 'index.html'), html)
  let contract = { contract_id: 'contract', revision: 1, requirement_revision: 1, candidates: {} }
  const posts = []
  const state = { acceptance: { id: 'contract', revision: 1 }, cfg: { workspace: join(base, 'control') },
    profile: { reviewer: { mode: 'model' } }, fixedTask: { candidate_paths: entries, output_kind: 'files' },
    implementers: new Map([['implementation', { item_id: 'implementation', worker_session_id: 'native-child' }]]),
    sidecar: { call: async (method, path, request) => {
      assert.equal(path, '/api/acceptance')
      if (method === 'GET') return { contracts: [structuredClone(contract)] }
      assert.equal(request.action, 'candidate'); posts.push(structuredClone(request))
      const candidate = structuredClone(request.payload)
      contract = { ...contract, revision: contract.revision + 1, current_candidate_id: candidate.candidate_id,
        candidates: { [candidate.candidate_id]: candidate } }
      return { ok: true, contract_id: contract.contract_id, revision: contract.revision, contract, candidate }
    } },
  }
  const runtime = new AcceptanceRuntime({}), parent = { session: { id: 'root', header: { cwd } } }
  return { base, cwd, state, posts, runtime, parent,
    capture: rows => runtime.capture(state, parent, rows), snapshot: () => posts.at(-1)?.payload.snapshot }
}

test('capture excludes only external native observations and retains ec26c449 write/edit provenance', async () => {
  const h = await fixture(), output = join(h.cwd, 'index.html'), outside = join(h.base, 'arbitrary-scratch', 'probe.data')
  // The external path deliberately does not exist: capture must not read it.
  const rows = [delivery([observation(output), observation(output, 'edit', 28),
    observation(outside, 'write', 49), observation(outside, 'edit', 54), observation(outside, 'edit', 56)])]
  const original = structuredClone(rows), result = await h.capture(rows), snapshot = h.snapshot()
  assert.deepEqual(snapshot.entry_paths, ['index.html'])
  assert.deepEqual(snapshot.changed_paths, ['index.html'])
  assert.deepEqual(snapshot.candidate_files.map(file => file.path), ['index.html'])
  assert.equal(snapshot.dependency_complete, true)
  assert.equal((await verifyCandidateSnapshot(snapshot)).ok, true)
  assert.equal(await readFile(join(snapshot.view_path, 'index.html'), 'utf8'), '<p>saved self-contained candidate</p>')
  assert.deepEqual(rows, original, 'original diagnostic evidence is neither removed nor rewritten')
  const observed = result.capture_observations
  assert.deepEqual(observed, h.state.acceptance.capture_observations)
  assert.equal(observed.excluded_count, 3); assert.equal(observed.exclusions_omitted, 0)
  assert.deepEqual(observed.exclusions.map(row => row.result_seq), [49, 54, 56])
  assert.ok(observed.exclusions.every(row => row.path === outside && row.source === 'native-successful-tool'
    && row.reason === 'native-observation-outside-workspace' && row.worker_session_id === 'native-child'))
  assert.equal(snapshot.capture_observations, undefined, 'diagnostic disclosure is outside immutable candidate metadata')
})

test('explicit outside entry is still rejected even when also present in excluded native observations', async () => {
  const h = await fixture({ entries: ['../external.html'] })
  await assert.rejects(h.capture([delivery([observation(join(h.base, 'external.html'))])]), { code: 'CANDIDATE_PATH_ESCAPE' })
  assert.equal(h.posts.length, 0)
})

for (const [name, overrides] of [['unknown provenance', { source: 'worker-report' }],
  ['missing provenance', { source: undefined }], ['truncated path', { path_truncated: true }],
  ['unrecognized operation', { operation: 'read' }]]) {
  test('outside diagnostic is not silently discarded for ' + name, async () => {
    const h = await fixture()
    await assert.rejects(h.capture([delivery([observation(join(h.base, 'outside.html'), 'write', 23, overrides)])]), { code: 'CANDIDATE_PATH_ESCAPE' })
    assert.equal(h.posts.length, 0)
  })
}

for (const [target, code] of [['bad?.html', 'CANDIDATE_PATH_INVALID'], ['.', 'CANDIDATE_PATH_ESCAPE'], ['bad\0.html', 'CANDIDATE_PATH_INVALID']]) {
  test('inside unsafe diagnostic path remains rejected: ' + JSON.stringify(target), async () => {
    const h = await fixture()
    await assert.rejects(h.capture([delivery([observation(target)])]), { code })
    assert.equal(h.posts.length, 0)
  })
}

for (const targetInside of [false, true]) {
  test('workspace link is rejected even when its destination is ' + (targetInside ? 'inside' : 'outside'), async t => {
    const h = await fixture(), target = join(targetInside ? h.cwd : h.base, 'destination')
    await mkdir(target); await writeFile(join(target, 'data.json'), '{"value":1}')
    try { await symlink(target, join(h.cwd, 'linked'), process.platform === 'win32' ? 'junction' : 'dir') }
    catch (error) { if (['EPERM', 'EACCES'].includes(error.code)) { t.skip('Host denies link creation'); return }; throw error }
    await assert.rejects(h.capture([delivery([observation('linked/data.json')])]), { code: 'CANDIDATE_LINK_UNSUPPORTED' })
    assert.equal(h.posts.length, 0)
  })
}

test('an actual outside dependency remains unknown even if the same path is an excluded observation', async () => {
  const h = await fixture({ html: '<script src="../outside.js"></script>' })
  const result = await h.capture([delivery([observation(join(h.base, 'outside.js'))])]), snapshot = h.snapshot()
  assert.equal(result.capture_observations.excluded_count, 1)
  assert.equal(snapshot.dependency_complete, false)
  assert.ok(snapshot.unknown_dependencies.some(row => row.path === 'index.html'
    && row.reference === '../outside.js' && row.reason === 'CANDIDATE_PATH_ESCAPE'))
  assert.equal((await verifyCandidateSnapshot(snapshot)).ok, true, 'complete bytes do not imply complete dependencies')
})

test('implicit entry fallback uses only workspace observations and cannot promote an outside report path', async () => {
  const h = await fixture({ entries: [] })
  await h.capture([delivery([observation(join(h.base, 'outside.dat')), observation('index.html')])])
  assert.deepEqual(h.snapshot().entry_paths, ['index.html'])
  const empty = await fixture({ entries: [] })
  await assert.rejects(empty.capture([delivery([observation(join(empty.base, 'outside.dat'))])]), { code: 'CANDIDATE_PATHS_REQUIRED' })
  assert.equal(empty.posts.length, 0)
})

test('outside disclosures are bounded while the original evidence stays complete', async () => {
  const h = await fixture(), rows = [delivery(Array.from({ length: 40 }, (_, i) => observation(join(h.base, 'out-' + i + '.dat'), 'write', i)))]
  const original = structuredClone(rows), result = await h.capture(rows), observed = result.capture_observations
  assert.equal(observed.excluded_count, 40); assert.equal(observed.exclusions.length, 32); assert.equal(observed.exclusions_omitted, 8)
  assert.deepEqual(rows, original)
  assert.deepEqual(h.snapshot().changed_paths, [])
})

test('Windows cross-drive native observation is excluded without opening the other drive', { skip: process.platform !== 'win32' }, async () => {
  const h = await fixture(), drive = parse(h.cwd).root[0].toUpperCase() === 'Z' ? 'Y' : 'Z'
  const outside = drive + ':\\not-mounted-observation\\helper.bin'
  const result = await h.capture([delivery([observation(outside)])])
  assert.equal(result.capture_observations.excluded_count, 1)
  assert.equal(result.capture_observations.exclusions[0].path, outside)
  assert.deepEqual(h.snapshot().candidate_files.map(file => file.path), ['index.html'])
})

test('relative outside native observation follows the same workspace boundary', async () => {
  const h = await fixture(), outside = join(h.base, 'arbitrary.file')
  const result = await h.capture([delivery([observation(relative(h.cwd, outside))])])
  assert.equal(result.capture_observations.excluded_count, 1)
  assert.deepEqual(h.snapshot().changed_paths, [])
})
