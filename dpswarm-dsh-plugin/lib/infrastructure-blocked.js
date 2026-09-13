import { createHash, createHmac, randomBytes, randomUUID, timingSafeEqual } from 'node:crypto'
import { closeSync, fsyncSync, mkdirSync, openSync, readFileSync, renameSync, statSync, unlinkSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'

// Only typed host-capability / protocol / durable-audit failures belong here. Worker,
// reviewer, budget, transport and arbitrary controller failures remain errors.
const CODES = new Set(['SIDECAR_RUNTIME_INCOMPATIBLE', 'HOST_RUNTIME_INCOMPATIBLE', 'PLUGIN_AUDIT_INVALID_EVENT',
  'PLUGIN_AUDIT_INVALID_READ', 'PLUGIN_AUDIT_INVALID_WRITE', 'PLUGIN_AUDIT_CORRUPT',
  'PLUGIN_AUDIT_MISSING', 'PLUGIN_AUDIT_CONTINUITY_LOST', 'PLUGIN_AUDIT_INCOMPLETE_COMMIT', 'PLUGIN_AUDIT_UNAVAILABLE', 'PLUGIN_AUDIT_IO_ERROR'])
export const isInfrastructureFailure = error => CODES.has(error?.code)
export const infrastructureAdmission = (error, fingerprint = null) => ({
  kind: 'infrastructure', blocked: true, code: error.code,
  message: String(error.message || error.code).slice(0, 2048),
  fingerprint: typeof error.details?.fingerprint === 'string' ? error.details.fingerprint
    : error.code === 'HOST_RUNTIME_INCOMPATIBLE'
      ? createHash('sha256').update(JSON.stringify({ code: error.code,
        component: error.details?.component ?? null, stage: error.details?.stage ?? null })).digest('hex')
      : fingerprint,
  observed_at: Date.now(),
})

const MAX_BYTES = 65536
const fail = () => Object.assign(new Error('TEAM_REQUIRED_BLOCKED_RECORD_INVALID: The host recovery record failed authentication.'), { code: 'TEAM_REQUIRED_BLOCKED_RECORD_INVALID' })
const digest = value => createHash('sha256').update(value).digest('hex')

/** A host-owned deny-only checkpoint when the authoritative audit cannot write.
 * It has no completion, acceptance, lock-release or write-authority semantics.
 * Native task binding and journal-derived starts are authenticated together.
 */
export class InfrastructureBlockedStore {
  constructor(directory) { this.directory = resolve(directory) }
  path(rootId) { return join(this.directory, digest(rootId) + '.json') }
  key(create = false) {
    const path = join(this.directory, 'host-auth.key')
    if (create) {
      mkdirSync(this.directory, { recursive: true, mode: 0o700 })
      try {
        const fd = openSync(path, 'wx', 0o600)
        try { writeFileSync(fd, randomBytes(32)); fsyncSync(fd) } finally { closeSync(fd) }
      } catch (error) { if (error.code !== 'EEXIST') throw error }
    }
    const key = readFileSync(path)
    if (key.length !== 32) throw fail()
    return key
  }
  read(binding) {
    const path = this.path(binding.root_session_id)
    let envelope
    try {
      if (statSync(path).size > MAX_BYTES) throw fail()
      envelope = JSON.parse(readFileSync(path, 'utf8'))
    } catch (error) { if (error.code === 'ENOENT') return null; throw fail() }
    const serialized = JSON.stringify(envelope.record)
    const expected = createHmac('sha256', this.key()).update(serialized).digest()
    const supplied = Buffer.from(typeof envelope.mac === 'string' ? envelope.mac : '', 'hex')
    if (supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) throw fail()
    const record = envelope.record
    if (record?.version !== 1 || record.state?.binding?.root_session_id !== binding.root_session_id
      || record.state?.admission?.kind !== 'infrastructure' || record.state.admission.blocked !== true
      || !CODES.has(record.state.admission.code) || record.state.phase !== 'blocked') throw fail()
    if (record.state.binding.binding_id !== binding.binding_id
      || record.state.binding.user_message_id !== binding.user_message_id
      || record.state.binding.user_content_sha256 !== binding.user_content_sha256) return null
    return record.state
  }
  write(state) {
    const record = { version: 1, state }, serialized = JSON.stringify(record)
    const key = this.key(true), mac = createHmac('sha256', key).update(serialized).digest('hex')
    const contents = JSON.stringify({ record, mac })
    if (Buffer.byteLength(contents) > MAX_BYTES) throw fail()
    const path = this.path(state.binding.root_session_id), temporary = path + '.' + randomUUID() + '.tmp'
    let fd
    try {
      fd = openSync(temporary, 'wx', 0o600); writeFileSync(fd, contents); fsyncSync(fd); closeSync(fd); fd = undefined
      renameSync(temporary, path)
    } finally {
      if (fd !== undefined) closeSync(fd)
      try { unlinkSync(temporary) } catch (error) { if (error.code !== 'ENOENT') throw error }
    }
  }
  clear(binding) {
    // Never delete another task's checkpoint during a late completion.
    if (!this.read(binding)) return
    unlinkSync(this.path(binding.root_session_id))
  }
}
