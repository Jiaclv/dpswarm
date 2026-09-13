import assert from 'node:assert/strict'
import test from 'node:test'
import { chmod, mkdtemp, mkdir, readFile, rename, symlink, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { candidateManifestDigest, collectCandidateSnapshot, materializeCandidateSnapshot, safeCandidatePath, verifyCandidateSnapshot } from '../lib/candidate-snapshot.js'

async function fixture() {
  const base = await mkdtemp(join(tmpdir(), 'dpswarm-candidate-')), cwd = join(base, 'work'), storageDir = join(base, 'sealed')
  await mkdir(join(cwd, 'assets'), { recursive: true })
  return { cwd, storageDir, base }
}

test('HTML candidate seals unchanged JS/CSS/image dependencies with their relative directories', async () => {
  const f = await fixture()
  await writeFile(join(f.cwd, 'index.html'), '<link rel="stylesheet" href="assets/style.css"><script src="assets/motion.js"></script>')
  await writeFile(join(f.cwd, 'assets/style.css'), 'body{background:url("dot.svg")}')
  await writeFile(join(f.cwd, 'assets/dot.svg'), '<svg/>')
  await writeFile(join(f.cwd, 'assets/motion.js'), 'const motion=1;')
  const first = await collectCandidateSnapshot({ ...f, entryPaths: ['index.html'], changedPaths: ['index.html'] })
  assert.equal(first.dependency_complete, true)
  assert.deepEqual(first.changed_paths, ['index.html'])
  assert.deepEqual(first.candidate_files.map(file => file.path), ['assets/dot.svg', 'assets/motion.js', 'assets/style.css', 'index.html'])
  assert.equal(await readFile(join(first.view_path, 'assets/motion.js'), 'utf8'), 'const motion=1;')
  await writeFile(join(f.cwd, 'assets/motion.js'), 'const motion=2;')
  const second = await collectCandidateSnapshot({ ...f, entryPaths: ['index.html'], changedPaths: ['index.html'] })
  assert.equal(first.candidate_files.find(file => file.path === 'index.html').sha256, second.candidate_files.find(file => file.path === 'index.html').sha256)
  assert.notEqual(first.manifest_digest, second.manifest_digest)
  const checked = await verifyCandidateSnapshot(first, { cwd: f.cwd })
  assert.equal(checked.ok, true)
  assert.equal(checked.workspace_matches, false)
})

test('missing, external and dynamic dependencies are explicit unknowns', async () => {
  const f = await fixture()
  await writeFile(join(f.cwd, 'index.html'), '<img src="missing.png"><script src="https://example.test/app.js"></script><script>fetch(resourceName)</script>')
  const candidate = await collectCandidateSnapshot({ ...f, entryPaths: ['index.html'] })
  assert.equal(candidate.dependency_complete, false)
  for (const reason of ['missing_dependency', 'external_dependency', 'dynamic_dependency']) assert.ok(candidate.unknown_dependencies.some(row => row.reason === reason), reason)
})

test('duplicate aliases, escape paths and junctions cannot enter a sealed candidate', async t => {
  const f = await fixture()
  await writeFile(join(f.cwd, 'index.html'), '<p>self contained</p>')
  await assert.rejects(collectCandidateSnapshot({ ...f, entryPaths: ['index.html', './index.html'] }), { code: 'CANDIDATE_DUPLICATE_PATH' })
  await assert.rejects(safeCandidatePath(f.cwd, '../outside.txt'), { code: 'CANDIDATE_PATH_ESCAPE' })
  const outside = join(f.base, 'outside'); await mkdir(outside); await writeFile(join(outside, 'secret.txt'), 'outside')
  try { await symlink(outside, join(f.cwd, 'linked'), process.platform === 'win32' ? 'junction' : 'dir') }
  catch (error) { if (['EPERM', 'EACCES'].includes(error.code)) { t.diagnostic('Host denies link creation'); return }; throw error }
  await assert.rejects(safeCandidatePath(f.cwd, 'linked/secret.txt'), { code: 'CANDIDATE_LINK_UNSUPPORTED' })
})

test('byte limits identify the unsealed dependency and cannot silently report complete', async () => {
  const f = await fixture()
  await writeFile(join(f.cwd, 'index.html'), '<script src="assets/a.js"></script>')
  await writeFile(join(f.cwd, 'assets/a.js'), 'x'.repeat(100))
  const candidate = await collectCandidateSnapshot({ ...f, entryPaths: ['index.html'], limits: { maxFileBytes: 50 } })
  assert.equal(candidate.dependency_complete, false)
  assert.ok(candidate.unknown_dependencies.some(row => row.path === 'assets/a.js' && row.reason === 'byte_limit'))
})

test('text is a real byte candidate and cannot substitute for file paths', async () => {
  const f = await fixture()
  const candidate = await collectCandidateSnapshot({ ...f, text: '完成的文本结果' })
  assert.equal(candidate.kind, 'text')
  assert.equal((await verifyCandidateSnapshot(candidate)).ok, true)
  assert.equal(candidate.manifest_digest, candidateManifestDigest(candidate))
  await assert.rejects(collectCandidateSnapshot({ ...f, text: 'report only', entryPaths: ['index.html'] }), { code: 'CANDIDATE_KIND_CONFLICT' })
})

test('mutated bytes, digest or view are rejected; same digest never overwrites a corrupt view', async () => {
  const f = await fixture()
  await writeFile(join(f.cwd, 'index.html'), '<p>original</p>')
  const candidate = await collectCandidateSnapshot({ ...f, entryPaths: ['index.html'] })
  const forged = structuredClone(candidate); forged.candidate_files[0].content_base64 = Buffer.from('forged').toString('base64')
  assert.equal((await verifyCandidateSnapshot(forged)).ok, false)
  const path = join(candidate.view_path, 'index.html'); await chmod(path, 0o644); await writeFile(path, '<p>tampered</p>')
  assert.equal((await verifyCandidateSnapshot(candidate)).ok, false)
  await assert.rejects(collectCandidateSnapshot({ ...f, entryPaths: ['index.html'] }), { code: 'CANDIDATE_VIEW_CORRUPT' })
})


test('constructed JavaScript resource loaders cannot claim a complete local snapshot', async () => {
  const f = await fixture()
  await writeFile(join(f.cwd, 'index.html'), '<script>new Function(code)(); window["fetch"](resource)</script>')
  const candidate = await collectCandidateSnapshot({ ...f, entryPaths: ['index.html'] })
  assert.equal(candidate.dependency_complete, false)
  assert.ok(candidate.unknown_dependencies.some(row => row.reason === 'dynamic_dependency'))
})


test('repeated managed edits become one changed path without duplicating the manifest', async () => {
  const f = await fixture()
  await writeFile(join(f.cwd, 'index.html'), '<p>after several edits</p>')
  const candidate = await collectCandidateSnapshot({ ...f, entryPaths: ['index.html'], changedPaths: ['index.html', './index.html', 'index.html'] })
  assert.deepEqual(candidate.changed_paths, ['index.html'])
  assert.equal(candidate.candidate_files.length, 1)
})


test('snapshot publication retries a transient Windows busy error without accepting a missing view', async () => {
  const f = await fixture(), original = await collectCandidateSnapshot({ ...f, text: 'ready bytes' })
  const candidate = { ...original }; delete candidate.view_path
  let attempts = 0
  const view = await materializeCandidateSnapshot(candidate, join(f.base, 'retry-view'), { renameView: async (from, to) => {
    if (++attempts === 1) throw Object.assign(new Error('temporarily locked'), { code: 'EPERM' })
    return rename(from, to)
  } })
  assert.equal(attempts, 2)
  assert.equal(await readFile(join(view, '__text__/delivery.txt'), 'utf8'), 'ready bytes')
  await assert.rejects(materializeCandidateSnapshot(candidate, join(f.base, 'never-published'), { renameView: async () => {
    throw Object.assign(new Error('still locked'), { code: 'EBUSY' })
  } }), error => error.code === 'CANDIDATE_PUBLISH_FAILED' && error.cause.code === 'EBUSY')
})


test('unsupported executable entry dependencies remain unknown while static data and opaque assets can seal', async () => {
  const f = await fixture()
  await writeFile(join(f.cwd, 'main.py'), 'import helper\nprint(helper.value)\n')
  const executable = await collectCandidateSnapshot({ ...f, entryPaths: ['main.py'] })
  assert.equal(executable.dependency_complete, false)
  assert.ok(executable.unknown_dependencies.some(row => row.path === 'main.py' && row.reason === 'unsupported_executable_entry_dependencies'))
  await writeFile(join(f.cwd, 'data.json'), '{"value":1}')
  assert.equal((await collectCandidateSnapshot({ ...f, entryPaths: ['data.json'] })).dependency_complete, true)
  await writeFile(join(f.cwd, 'index.html'), '<img src="assets/opaque.png">')
  await writeFile(join(f.cwd, 'assets/opaque.png'), Buffer.from([1, 2, 3]))
  assert.equal((await collectCandidateSnapshot({ ...f, entryPaths: ['index.html'] })).dependency_complete, true)
})
