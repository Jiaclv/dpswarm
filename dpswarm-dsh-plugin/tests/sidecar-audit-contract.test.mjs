import assert from 'node:assert/strict'
import test from 'node:test'
import { mkdtempSync, writeFileSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Sidecar } from '../lib/sidecar.js'
import { FixedTeamController } from '../lib/fixed-team.js'

test('audit-required sidecar rejects a listening old host without starting a second process', async () => {
  const sidecar = new Sidecar({ sidecarUrl: 'http://127.0.0.1:8799', sessionIsolation: true, auditJournalRequired: true })
  let starts = 0
  sidecar.call = async () => ({ bridge: { session_isolation: true } })
  sidecar._start = async () => { starts++ }
  await assert.rejects(sidecar.ensure(), { code: 'SIDECAR_AUDIT_VERSION_MISMATCH' })
  assert.equal(starts, 0)
  sidecar.call = async () => ({ bridge: { session_isolation: true, plugin_audit_v1: true } })
  await sidecar.ensure()
  assert.equal(starts, 0)
})

test('ordinary control connections retain compatibility when audit was not requested', async () => {
  const sidecar = new Sidecar({ sidecarUrl: 'http://127.0.0.1:8799', sessionIsolation: true })
  sidecar.call = async () => ({ bridge: { session_isolation: true } })
  await sidecar.ensure()
})

function leaseFixture(finishRun) {
  const directory = mkdtempSync(join(tmpdir(), 'dpswarm-audit-release-'))
  const path = join(directory, 'lease.json'), lease = { run_id: 'run', session_id: 'root', path }
  writeFileSync(path, JSON.stringify(lease))
  const controller = new FixedTeamController({ config: () => ({}), cm: { finishRun } })
  return { controller, path, state: { lease, parentSession: { id: 'root' } } }
}

test('workspace stays leased until CM lifecycle closure is durable', async () => {
  let resolve
  const barrier = new Promise(r => { resolve = r })
  const h = leaseFixture(async (root, session) => { assert.equal(root, 'root'); assert.equal(session.id, 'root'); await barrier })
  const release = h.controller.release(h.state)
  assert.ok(existsSync(h.path)); assert.ok(h.state.lease)
  resolve(); await release
  assert.equal(existsSync(h.path), false); assert.equal(h.state.lease, null)
})

test('failed CM audit closure leaves the original workspace lease recoverable', async () => {
  const h = leaseFixture(async () => { throw Object.assign(new Error('fixture'), { code: 'AUDIT_PERSIST_FAILED' }) })
  await assert.rejects(h.controller.release(h.state), { code: 'AUDIT_PERSIST_FAILED' })
  assert.ok(existsSync(h.path)); assert.ok(h.state.lease)
})
