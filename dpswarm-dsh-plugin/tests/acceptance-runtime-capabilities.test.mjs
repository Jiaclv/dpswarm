import assert from 'node:assert/strict'
import test from 'node:test'
import { Sidecar, ACCEPTANCE_RUNTIME_REQUIREMENTS, acceptanceRuntimeCompatibility } from '../lib/sidecar.js'

test('strict acceptance capability is additive and does not block old status or cleanup connections', async () => {
  const old = { bridge: { session_isolation: true, plugin_audit_v1: true } }
  const sidecar = new Sidecar({ sidecarUrl: 'http://127.0.0.1:8799', sessionIsolation: true, auditJournalRequired: true })
  const calls = []
  sidecar.call = async (...args) => { calls.push(args); return old }
  sidecar._start = async () => assert.fail('Read-only compatibility checks must not launch a service')
  await sidecar.ensure()
  await assert.rejects(sidecar.requireAcceptanceCapabilities(), error => {
    assert.equal(error.code, 'SIDECAR_RUNTIME_INCOMPATIBLE')
    assert.deepEqual(error.details.missing, ['runtime.acceptance_contract'])
    return true
  })
  assert.equal(calls.every(([method, path]) => method === 'GET' && path === '/api/status'), true)
  await sidecar.ensure()
})

test('matching acceptance capability is checked independently of the legacy global revision', () => {
  const health = { bridge: { runtime: { ...ACCEPTANCE_RUNTIME_REQUIREMENTS, revision: 1 } } }
  const first = acceptanceRuntimeCompatibility(health)
  health.bridge.runtime.revision = 999
  health.bridge.runtime.package_version = 'unrelated'
  assert.equal(acceptanceRuntimeCompatibility(health).fingerprint, first.fingerprint)
  health.bridge.runtime.acceptance_contract = 'unsupported-v2'
  assert.throws(() => acceptanceRuntimeCompatibility(health), { code: 'SIDECAR_RUNTIME_INCOMPATIBLE' })
})
