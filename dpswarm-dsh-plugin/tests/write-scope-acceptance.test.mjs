import assert from 'node:assert/strict'
import test from 'node:test'
import { chmod, mkdtemp, mkdir, readFile, stat, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { collectCandidateSnapshot } from '../lib/candidate-snapshot.js'
import { readEvidence } from '../lib/evidence-reader.js'
import { WriteScopeRegistry, installWriteScope } from '../lib/write-scope.js'
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'

async function fixture({ entryPath = 'a.html', content = '<p>A1</p>' } = {}) {
  const base = await mkdtemp(join(tmpdir(), 'dpswarm-ready-')), cwd = join(base, 'work'), storageDir = join(base, 'sealed')
  await mkdir(cwd)
  const handlers = [], journal = new MemoryAuditJournal(), registry = new WriteScopeRegistry({ journal })
  installWriteScope({ on: (event, fn) => { handlers.push(fn); return () => {} } }, registry)
  await registry.claim({ rootId: 'root', sessionId: 'wa', subtask: 'a', scopes: [entryPath] })
  await registry.claim({ rootId: 'root', sessionId: 'wb', subtask: 'b', scopes: ['b.html'] })
  registry.registerArtifact('root', { id: 'a', write_globs: [entryPath], state: 'draft', version: 1, acceptance_contract: 'dpswarm-acceptance-v1' })
  const exec = (id, name, path) => ({ agent: { session: { id, header: { cwd } } }, name, args: { file_path: path } })
  await writeFile(join(cwd, entryPath), content)
  const candidate = await collectCandidateSnapshot({ cwd, storageDir, entryPaths: [entryPath], changedPaths: [entryPath] })
  await registry.bindReadyManifest('root', 'a', candidate)
  registry.updateArtifactState('root', 'a', 'ready', 2)
  return { cwd, storageDir, registry, journal, candidate, exec, hook: handlers[0] }
}

test('ready refuses managed mutation and downstream reads sealed A1 even if mutable workspace is A2', async () => {
  const f = await fixture()
  await assert.rejects(f.hook(f.exec('wa', 'edit', 'a.html'), async () => {}), { code: 'ARTIFACT_READY_IMMUTABLE' })
  await writeFile(join(f.cwd, 'a.html'), '<p>A2 external edit</p>')
  const request = f.exec('wb', 'read', 'a.html')
  const actual = await f.hook(request, () => readFile(request.args.file_path, 'utf8'))
  assert.equal(actual, '<p>A1</p>')
  assert.equal(request.args.file_path, 'a.html', 'tool envelope restored after its call')
  const refs = f.registry.consumedManifestRefsFor('root', 'wb')
  assert.equal(refs.length, 1)
  assert.equal(refs[0].manifest_digest, f.candidate.manifest_digest)
  assert.deepEqual(refs[0].paths, ['a.html'])
  await f.hook(request, () => readFile(request.args.file_path, 'utf8'))
  assert.equal(f.registry.consumedManifestRefsFor('root').length, 1)
})

test('moving ready to draft retires its current seal; resealing publishes a different consumed version', async () => {
  const f = await fixture()
  f.registry.updateArtifactState('root', 'a', 'draft', 3)
  assert.equal(f.registry.artifactsFor('root')[0].ready_manifest, undefined)
  await f.hook(f.exec('wa', 'edit', 'a.html'), () => writeFile(join(f.cwd, 'a.html'), '<p>A2</p>'))
  await assert.rejects(f.hook(f.exec('wb', 'read', 'a.html'), async () => {}), { code: 'ARTIFACT_NOT_READY' })
  const candidate = await collectCandidateSnapshot({ cwd: f.cwd, storageDir: f.storageDir, entryPaths: ['a.html'] })
  assert.notEqual(candidate.manifest_digest, f.candidate.manifest_digest)
  await f.registry.bindReadyManifest('root', 'a', candidate)
  f.registry.updateArtifactState('root', 'a', 'ready', 4)
  const request = f.exec('wb', 'read', 'a.html')
  assert.equal(await f.hook(request, () => readFile(request.args.file_path, 'utf8')), '<p>A2</p>')
  assert.equal(f.registry.consumedManifestRefsFor('root')[0].manifest_digest, candidate.manifest_digest)
})

test('tampered ready view and broad grep are refused without recording consumption', async () => {
  const f = await fixture()
  await assert.rejects(f.hook(f.exec('wb', 'grep', '.'), async () => {}), { code: 'ARTIFACT_SNAPSHOT_TARGET_REQUIRED' })
  const path = join(f.candidate.view_path, 'a.html'); await chmod(path, 0o644); await writeFile(path, 'tampered')
  await assert.rejects(f.hook(f.exec('wb', 'read', 'a.html'), async () => {}), { code: 'ARTIFACT_MANIFEST_INVALID' })
  assert.deepEqual(f.registry.consumedManifestRefsFor('root'), [])
})


test('reading the explicit snapshot view records its fixed version and rejects writes to it', async () => {
  const f = await fixture()
  const target = join(f.candidate.view_path, 'a.html'), request = f.exec('wb', 'read', target)
  assert.equal(await f.hook(request, () => readFile(request.args.file_path, 'utf8')), '<p>A1</p>')
  assert.equal(f.registry.consumedManifestRefsFor('root')[0].manifest_digest, f.candidate.manifest_digest)
  await assert.rejects(f.hook(f.exec('wa', 'write', target), async () => {}), { code: 'ARTIFACT_SNAPSHOT_IMMUTABLE' })
  await assert.rejects(f.hook({ ...f.exec('wb', 'grep', '.'), args: { pattern: 'A1' } }, async () => {}), { code: 'ARTIFACT_SNAPSHOT_TARGET_REQUIRED' })
})

test('unchanged dependencies outside upstream write scope are consumed from its ready snapshot', async () => {
  const f = await fixture()
  f.registry.updateArtifactState('root', 'a', 'draft', 3)
  await writeFile(join(f.cwd, 'a.html'), '<script src="motion.js"></script>')
  await writeFile(join(f.cwd, 'motion.js'), 'const motion=1;')
  const candidate = await collectCandidateSnapshot({ cwd: f.cwd, storageDir: f.storageDir, entryPaths: ['a.html'] })
  await f.registry.bindReadyManifest('root', 'a', candidate)
  f.registry.updateArtifactState('root', 'a', 'ready', 4)
  await writeFile(join(f.cwd, 'motion.js'), 'const motion=2;')
  const request = f.exec('wb', 'read', 'motion.js')
  assert.equal(await f.hook(request, () => readFile(request.args.file_path, 'utf8')), 'const motion=1;')
  assert.deepEqual(f.registry.consumedManifestRefsFor('root')[0].paths, ['motion.js'])
})


test('binding a snapshot does not publish a ready handoff before the authoritative transition', async () => {
  const f = await fixture()
  f.registry.updateArtifactState('root', 'a', 'draft', 3)
  await writeFile(join(f.cwd, 'a.html'), '<p>A2 pending</p>')
  const candidate = await collectCandidateSnapshot({ cwd: f.cwd, storageDir: f.storageDir, entryPaths: ['a.html'] })
  await f.registry.bindReadyManifest('root', 'a', candidate)
  assert.equal(f.registry.sealedArtifactsFor('root').some(row => row.manifest_digest === candidate.manifest_digest), false)
  await assert.rejects(f.hook(f.exec('wb', 'read', join(candidate.view_path, 'a.html')), async () => {}), { code: 'ARTIFACT_NOT_READY' })
  await assert.rejects(f.hook(f.exec('wb', 'read', 'a.html'), async () => {}), { code: 'ARTIFACT_NOT_READY' })
  f.registry.updateArtifactState('root', 'a', 'ready', 4)
  assert.equal(f.registry.sealedArtifactsFor('root').some(row => row.manifest_digest === candidate.manifest_digest), true)
})


test('an explicit tool-result read error is not registered as consumed evidence', async () => {
  const f = await fixture(), request = f.exec('wb', 'read', 'a.html')
  const failure = { content: [{ type: 'tool-result', isError: true, output: 'Read unavailable' }] }
  assert.equal(await f.hook(request, async () => failure), failure)
  assert.deepEqual(f.registry.consumedManifestRefsFor('root'), [])
  assert.equal(request.args.file_path, 'a.html')
})


test('evidence reader cannot read staged draft or unpublished snapshot bytes', async () => {
  const f = await fixture({ entryPath: 'measurements.json', content: '{"pixels":0}' })
  f.registry.updateArtifactState('root', 'a', 'draft', 3)
  await writeFile(join(f.cwd, 'measurements.json'), '{"pixels":8}')
  const pending = await collectCandidateSnapshot({ cwd: f.cwd, storageDir: f.storageDir, entryPaths: ['measurements.json'] })
  await f.registry.bindReadyManifest('root', 'a', pending)
  let executed = false
  for (const path of ['measurements.json', join(pending.view_path, 'measurements.json')]) {
    await assert.rejects(f.hook(f.exec('wb', 'dpswarm_read_evidence', path), async () => { executed = true }), { code: 'ARTIFACT_NOT_READY' })
  }
  assert.equal(executed, false)
  assert.deepEqual(f.registry.consumedManifestRefsFor('root'), [])
})

test('evidence reader observes the ready immutable version and records only successful consumption', async () => {
  const f = await fixture({ entryPath: 'measurements.json', content: '{"pixels":0}' })
  await writeFile(join(f.cwd, 'measurements.json'), '{"pixels":8}')
  const ctx = { fs: {
    async resolve(path) { return { displayPath: path } },
    async stat(target) { const info = await stat(target.displayPath); return { type: info.isFile() ? 'file' : 'other', size: info.size } },
    async readBytes(target) { return readFile(target.displayPath) },
  } }
  const request = f.exec('wb', 'dpswarm_read_evidence', 'measurements.json')
  request.args.pointer = '/pixels'
  request.args.expected_sha256 = '0'.repeat(64)
  const call = () => f.hook(request, () => readEvidence(ctx, request.args, request))
  await assert.rejects(call(), { code: 'EVIDENCE_CHANGED' })
  assert.deepEqual(f.registry.consumedManifestRefsFor('root'), [])
  assert.equal(request.args.file_path, 'measurements.json')
  delete request.args.expected_sha256
  const page = await call()
  assert.equal(page.value, 0, 'the mutable workspace value 8 is not the published artifact')
  assert.equal(page.path, join(f.candidate.view_path, 'measurements.json'))
  assert.equal(request.args.file_path, 'measurements.json')
  const refs = f.registry.consumedManifestRefsFor('root', 'wb')
  assert.equal(refs.length, 1)
  assert.equal(refs[0].manifest_digest, f.candidate.manifest_digest)
  assert.deepEqual(refs[0].paths, ['measurements.json'])
  request.args.expected_sha256 = page.sha256
  assert.equal((await call()).value, 0)
  assert.deepEqual(f.registry.consumedManifestRefsFor('root', 'wb'), refs)
})
