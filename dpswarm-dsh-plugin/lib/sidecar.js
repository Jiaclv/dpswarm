import { spawn } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

export function sidecarSpawnSpec(cfg) {
  const url = new URL(cfg.sidecarUrl)
  return { command: cfg.pythonCmd, args: ['-m', 'dpswarm.server', '--port', String(Number(url.port) || 80)],
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

  async ensure() {
    if (await this.probe()) return
    if (!this.cfg.autoStart) {
      throw new Error(`DPSwarm sidecar 未启动（${this.cfg.sidecarUrl}）。`
        + ` 启动：cd ${this.cfg.dpswarmDir || '<dpswarm-plugin 目录>'} && python -m dpswarm.server`)
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
        join(this.cfg.dpswarmDir || '.', '.dpswarm-panel', '.dpswarm-token'),
        'utf8').trim()
    } catch (e) { this._tok = '' }
    return this._tok
  }

  async call(method, path, body, timeoutMs = 20000, _retried = false) {
    const ctrl = new AbortController()
    const timer = setTimeout(() => ctrl.abort(), timeoutMs)
    try {
      const headers = {}
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
        throw err
      }
      if (!res.ok) throw new Error(`sidecar ${path} → HTTP ${res.status}`)
      return json
    } finally {
      clearTimeout(timer)
    }
  }
}

