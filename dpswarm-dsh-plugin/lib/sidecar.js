import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const starting = new Map()

// This is a bounded wire contract, independent of package/install versions.
export const FIXED_TEAM_RUNTIME_REQUIREMENTS = Object.freeze({
  schema: 'dpswarm-runtime-capabilities-v1', revision: 1,
  audit_schema: 'dpswarm-plugin-audit-v1',
  audit_event_types: Object.freeze([
    'dpswarm/verification-required', 'dpswarm/verification-binding',
    'dpswarm/verification-superseded', 'dpswarm/verification-takeover',
    'dpswarm/verification-recovery',
    'dpswarm/worker-budget-team-run-resumed', 'dpswarm/worker-budget-team-run-resume-ended',
    'dpswarm/team-required-admission', 'dpswarm/team-required-continued',
  ]),
  admission_cleanup: 'delegate-admission-cleanup-v1',
})

// Additive and opt-in: new acceptance cannot invalidate legacy status or cleanup.
export const ACCEPTANCE_RUNTIME_REQUIREMENTS = Object.freeze({ acceptance_contract: 'dpswarm-acceptance-v1' })

export function acceptanceRuntimeCompatibility(health) {
  const required = ACCEPTANCE_RUNTIME_REQUIREMENTS
  const observed = { acceptance_contract: health?.bridge?.runtime?.acceptance_contract ?? null }
  const fingerprint = createHash('sha256').update(JSON.stringify({ required, observed })).digest('hex')
  if (observed.acceptance_contract !== required.acceptance_contract) {
    const error = new Error('SIDECAR_RUNTIME_INCOMPATIBLE: new strict tasks require dpswarm-acceptance-v1; update the matching service. Legacy status, reports and cleanup remain available.')
    error.code = 'SIDECAR_RUNTIME_INCOMPATIBLE'
    error.details = { ok: false, error: error.code, required, observed, missing: ['runtime.acceptance_contract'], fingerprint }
    throw error
  }
  return { compatible: true, fingerprint, runtime: health.bridge.runtime }
}

function runtimeCompatibility(health) {
  const required = FIXED_TEAM_RUNTIME_REQUIREMENTS
  const runtime = health?.bridge?.runtime
  const auditTypes = Array.isArray(runtime?.audit_event_types) ? runtime.audit_event_types : []
  const observed = {
    session_isolation: health?.bridge?.session_isolation === true,
    plugin_audit_v1: health?.bridge?.plugin_audit_v1 === true,
    schema: typeof runtime?.schema === 'string' ? runtime.schema : null,
    revision: Number.isSafeInteger(runtime?.revision) ? runtime.revision : null,
    audit_schema: typeof runtime?.audit_schema === 'string' ? runtime.audit_schema : null,
    audit_event_types: required.audit_event_types.filter(type => auditTypes.includes(type)),
    admission_cleanup: typeof runtime?.admission_cleanup === 'string' ? runtime.admission_cleanup : null,
  }
  const missing = []
  for (const key of ['session_isolation', 'plugin_audit_v1']) {
    if (!observed[key]) missing.push(`bridge.${key}`)
  }
  for (const key of ['schema', 'revision', 'audit_schema', 'admission_cleanup']) {
    if (observed[key] !== required[key]) missing.push(`runtime.${key}`)
  }
  for (const type of required.audit_event_types) {
    if (!observed.audit_event_types.includes(type)) missing.push(`audit_event_type:${type}`)
  }
  // Fixed construction order and requirement-order filtering make this canonical.
  // PID/start_id, timestamps and unrelated additive capabilities never reset a gate.
  const fingerprint = createHash('sha256').update(JSON.stringify({ required, observed })).digest('hex')
  if (missing.length) {
    const error = new Error('SIDECAR_RUNTIME_INCOMPATIBLE: the running control service lacks required fixed-team capabilities; update and restart the matching service before retrying.')
    error.code = 'SIDECAR_RUNTIME_INCOMPATIBLE'
    error.details = { ok: false, error: error.code, required, observed, missing, fingerprint }
    throw error
  }
  return { compatible: true, fingerprint, runtime }
}

export function sidecarSpawnSpec(cfg) {
  const url = new URL(cfg.sidecarUrl)
  const args = ['-m', cfg.sessionIsolation ? 'dpswarm.session_server' : 'dpswarm.server', '--port', String(Number(url.port) || 80)]
  if (cfg.workspace) args.push('--workspace', cfg.workspace)
  return { command: cfg.pythonCmd, args,
    options: { cwd: cfg.dpswarmDir || undefined, detached: true, stdio: 'ignore',
      windowsHide: true, shell: false } }
}

/** Sidecar 会话：探测、自动拉起、带超时 JSON 调用。 */
export class Sidecar {
  constructor(cfg) {
    const url = new URL(cfg.sidecarUrl)
    if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(url.hostname)
        || url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
      throw new Error('DPSwarm sidecarUrl must be a local HTTP origin')
    }
    this.cfg = { ...cfg, sidecarUrl: url.origin }
  }

  _checkCapabilities(health) {
    if (this.cfg.runtimeCapabilitiesRequired) runtimeCompatibility(health)
    if (this.cfg.hostCatalogRequired && health.bridge?.host_catalog_v1 !== true) {
      const error = new Error('SIDECAR_HOST_CATALOG_VERSION_MISMATCH: update the DPSwarm control service to use DPH model registration.')
      error.code = 'SIDECAR_HOST_CATALOG_VERSION_MISMATCH'
      throw error
    }
    if (this.cfg.sessionIsolation && health.bridge?.session_isolation !== true) {
      throw new Error('SIDECAR_VERSION_MISMATCH: this port runs the old single-session sidecar; use a separate port or restart it with dpswarm.session_server')
    }
    if (this.cfg.auditJournalRequired && health.bridge?.plugin_audit_v1 !== true) {
      const error = new Error('SIDECAR_AUDIT_VERSION_MISMATCH: this sidecar does not support the durable plugin audit ledger; install the matching version before starting limited workers or CM')
      error.code = 'SIDECAR_AUDIT_VERSION_MISMATCH'
      throw error
    }
  }

  /** Read-only fixed-team preflight. Never starts a service or reserves work. */
  async requireRuntimeCapabilities() {
    return runtimeCompatibility(await this.call('GET', '/api/status', undefined, 2500))
  }

  /** Strict acceptance checks are requested by new-task preflight, never globally. */
  async requireAcceptanceCapabilities() {
    return acceptanceRuntimeCompatibility(await this.call('GET', '/api/status', undefined, 2500))
  }

  async ensure() {
    let health
    try { health = await this.call('GET', '/api/status', undefined, 2500) } catch { /* May not yet be listening. */ }
    if (health) {
      this._checkCapabilities(health)
      return
    }
    const key = this.cfg.sidecarUrl
    if (starting.has(key)) { await starting.get(key); return this.ensure() }
    const pending = this._start()
    starting.set(key, pending)
    try { await pending } finally { starting.delete(key) }
    const ready = await this.call('GET', '/api/status')
    this._checkCapabilities(ready)
  }

  async _start() {
    if (!this.cfg.autoStart) {
      throw new Error(`DPSwarm sidecar 未启动（${this.cfg.sidecarUrl}）。`
        + ' 请运行安装自检，或按插件说明启动与当前版本匹配的控制服务。')
    }
    const launch = sidecarSpawnSpec(this.cfg)
    const child = spawn(launch.command, launch.args, launch.options)
    let launchError
    child.once('error', error => { launchError = error })
    child.unref()
    for (let i = 0; i < 30; i++) {
      await new Promise(r => setTimeout(r, 500))
      if (launchError) throw launchError
      if (await this.probe()) return
    }
    throw new Error(`DPSwarm sidecar 自动拉起失败（python ${this.cfg.pythonCmd}，`
      + `dir=${this.cfg.dpswarmDir}）。请手动启动后重试。`)
  }

  async probe() {
    try { return (await this.call('GET', '/api/status', undefined, 2500)) !== undefined }
    catch { return false }
  }

  /** 写接口 bearer token：sidecar 启动时写 workspace/.dpswarm-token（P0 修复配套）。
   *  autoStart 与手动启动共用同一 workspace 约定（README），按约定路径读取。
   *  读不到不缓存（sidecar 可能刚被拉起还没写完），下一次调用再试。 */
  _token() {
    if (this._tok) return this._tok
    try {
      this._tok = readFileSync(
        join(this.cfg.workspace || join(this.cfg.dpswarmDir || '.', '.dpswarm-panel'), '.dpswarm-token'),
        'utf8').trim()
    } catch (e) { this._tok = '' }
    return this._tok
  }

  async call(method, path, body, timeoutMs = 20000, _retried = false) {
    const ctrl = new AbortController()
    const timer = setTimeout(() => ctrl.abort(), timeoutMs)
    try {
      const headers = {}
      if (this.cfg.sessionId) headers['X-DPSwarm-Session'] = this.cfg.sessionId
      if (body !== undefined) headers['Content-Type'] = 'application/json'
      const tok = this._token()
      if (tok) headers.Authorization = 'Bearer ' + tok
      const res = await fetch(this.cfg.sidecarUrl + path, {
        method,
        signal: ctrl.signal,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
      })
      // sidecar 换过 token（workspace 重建）→ 清缓存重试一次（防无限递归）
      if (res.status === 401 && !_retried) {
        this._tok = ''
        return this.call(method, path, body, timeoutMs, true)
      }
      const text = await res.text()
      let json
      try { json = JSON.parse(text) } catch { throw new Error(`sidecar ${path} 非 JSON: ${text.slice(0, 200)}`) }
      if (!res.ok && json?.ok === false && json?.error) {
        const err = new Error(`${json.error}${json.message ? ' — ' + json.message : ''}`)
        err.code = json.error
        err.details = json
        throw err
      }
      if (!res.ok) throw new Error(`sidecar ${path} → HTTP ${res.status}`)
      return json
    } finally {
      clearTimeout(timer)
    }
  }
}

