import assert from 'node:assert/strict'
import test from 'node:test'
import { mkdtempSync, mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { WriteScopeRegistry, installWriteScope, scopeAllows, scopesOverlap, scopeRelativePath } from '../lib/write-scope.js'
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'

const cases = [
  ['src/a/**', 'src/a/x/y.js', true],
  ['src/a/**', 'src/b/x.js', false],
  ['a/*', 'a/b', true],
  ['a/*', 'a/b/c', false],
  ['a/b.txt', 'a/b.txt', true],
  ['a/b.txt', 'a/b.txt.bak', false],
  ['**', 'anything/at/all.js', true],
]
test('scope glob matching table', () => {
  for (const [glob, path, expected] of cases) assert.equal(scopeAllows([glob], path), expected, `${glob} vs ${path}`)
})

test('overlap detection is conservative and catches the decidable cases', () => {
  assert.equal(scopesOverlap(['a/**'], ['a/**']), true)
  assert.equal(scopesOverlap(['a/**'], ['a/b/**']), true)
  assert.equal(scopesOverlap(['a/b/**'], ['a/**']), true)
  assert.equal(scopesOverlap(['a/*'], ['a/b']), true)
  assert.equal(scopesOverlap(['a/x.js'], ['a/x.js']), true)
  assert.equal(scopesOverlap(['a/**'], ['b/**']), false)
  assert.equal(scopesOverlap(['a/x.js'], ['a/y.js']), false)
})

test('scopeRelativePath normalizes against cwd and rejects escapes', () => {
  const cwd = resolve(join(mkdtempSync(join(tmpdir(), 'dpswarm-scope-')), 'proj')); mkdirSync(cwd)
  assert.equal(scopeRelativePath(cwd, join(cwd, 'src', 'a.js')), 'src/a.js')
  assert.equal(scopeRelativePath(cwd, 'src/a.js'), 'src/a.js')
  assert.equal(scopeRelativePath(cwd, join(cwd, '..', 'outside.js')), null)
  assert.equal(scopeRelativePath(cwd, ''), null)
})

function hookFixture() {
  const handlers = []
  const ctx = { on: (name, fn) => { handlers.push({ name, fn }); return () => {} } }
  const journal = new MemoryAuditJournal()
  const registry = new WriteScopeRegistry({ journal })
  installWriteScope(ctx, registry)
  const cwd = resolve(join(mkdtempSync(join(tmpdir(), 'dpswarm-scope-')), 'proj')); mkdirSync(cwd)
  const worker = id => ({ session: { id, header: { id, parentSession: 'root', origin: 'subagent', delegationDepth: 1, cwd } } })
  const exec = (agent, name, args) => ({ agent, name, args, signal: new AbortController().signal })
  return { handlers, journal, registry, cwd, worker, exec }
}

test('claimed workers can write in scope, are denied outside, and reads are never gated', async () => {
  const h = hookFixture()
  const hook = h.handlers.find(r => r.name === 'tools/pre-execute').fn
  await h.registry.claim({ rootId: 'root', sessionId: 'w1', subtask: 'part-a', scopes: ['src/a/**'] })
  let proceeded = 0
  const next = async () => { proceeded++ }
  await hook(h.exec(h.worker('w1'), 'write', { file_path: join(h.cwd, 'src', 'a', 'x.js') }), next)
  assert.equal(proceeded, 1)
  await hook(h.exec(h.worker('w1'), 'read', { file_path: ' anywhere.txt' }), next)
  assert.equal(proceeded, 2, 'reads are never gated')
  await assert.rejects(hook(h.exec(h.worker('w1'), 'edit', { file_path: join(h.cwd, 'src', 'b', 'x.js') }), next), { code: 'WORKER_SCOPE_VIOLATION' })
  await assert.rejects(hook(h.exec(h.worker('w1'), 'write', { file_path: join(h.cwd, 'unowned.txt') }), next), { code: 'WORKER_SCOPE_VIOLATION' })
  await assert.rejects(hook(h.exec(h.worker('w1'), 'write', {})), { code: 'WORKER_SCOPE_TARGET_REQUIRED' })
  // Unclaimed sessions (serial mode, tester, reviewer) are untouched.
  await hook(h.exec(h.worker('w-other'), 'write', { file_path: join(h.cwd, 'src', 'b', 'x.js') }), next)
  assert.equal(proceeded, 3)
  const events = (await h.journal.read('root')).events.filter(e => e.type === 'dpswarm/write-scope')
  assert.equal(events.length, 1)
  assert.deepEqual(events[0].data.scopes, ['src/a/**'])
  assert.equal(events[0].data.subtask, 'part-a')
})

test('claim rejects overlap with an existing sibling claim and clears per run', async () => {
  const h = hookFixture()
  await h.registry.claim({ rootId: 'root', sessionId: 'w1', subtask: 'part-a', scopes: ['src/a/**'] })
  await assert.rejects(
    h.registry.claim({ rootId: 'root', sessionId: 'w2', subtask: 'part-b', scopes: ['src/a/b/**'] }),
    { code: 'WORKER_SCOPE_OVERLAP' })
  await h.registry.claim({ rootId: 'root', sessionId: 'w2', subtask: 'part-b', scopes: ['src/b/**'] })
  assert.equal(h.registry.claimsFor('root').length, 2)
  h.registry.clear('root')
  assert.equal(h.registry.claimsFor('root').length, 0)
  assert.equal(h.registry.forSession('w1'), null)
  await assert.rejects(
    h.registry.claim({ rootId: 'root', sessionId: 'w3', subtask: 'empty', scopes: [] }),
    { code: 'WORKER_SCOPE_INVALID' })
})

test('v3-P0: claim records carry state/version so the artifact entity can extend them later', async () => {
  const journal = new MemoryAuditJournal()
  const registry = new WriteScopeRegistry({ journal })
  const record = await registry.claim({ rootId: 'root', sessionId: 'w1', subtask: 'part-a', scopes: ['src/a/**'] })
  assert.equal(record.state, 'claimed')
  assert.equal(record.version, 1)
  const events = (await journal.read('root')).events.filter(e => e.type === 'dpswarm/write-scope')
  assert.equal(events[0].data.state, 'claimed')
  assert.equal(events[0].data.version, 1)
  // Legacy-shaped records (no state/version, as written by 0.8.0) still read back.
  const legacy = { root_session_id: 'root', worker_session_id: 'w-old', subtask: 'legacy', scopes: ['old/**'], run_id: null, claimed_at: 1 }
  registry.bySession.set('w-old', legacy)
  assert.equal(registry.forSession('w-old').subtask, 'legacy')
  assert.equal(registry.forSession('w-old').state, undefined)
})
