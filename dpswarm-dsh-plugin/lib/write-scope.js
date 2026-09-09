import { isAbsolute, relative, resolve, sep } from 'node:path'

const failure = (code, message) => Object.assign(new Error(`${code}: ${message}`), { code })

/** Tools whose file target is enforced against a worker's claimed write scope. */
const GATED_TOOLS = new Set(['write', 'edit'])

const SEP = process.platform === 'win32' ? /\\/g : null
const norm = value => {
  let out = String(value)
  if (SEP) out = out.replace(SEP, '/').toLowerCase()
  return out.replace(/\/+$/, '')
}

/** Resolve a tool-call target against the session cwd; null when it escapes. */
export function scopeRelativePath(cwd, target) {
  if (typeof target !== 'string' || !target.trim()) return null
  const absolute = isAbsolute(target) ? resolve(target) : resolve(cwd, target)
  const rel = relative(cwd, absolute)
  if (!rel) return ''
  if (rel === '..' || rel.startsWith('..' + sep) || isAbsolute(rel)) return null
  return norm(rel)
}

/** Minimal glob: `**` crosses directories, `*` stays within one segment. */
export function globToRegExp(glob) {
  const source = norm(glob)
  let out = '^', i = 0
  while (i < source.length) {
    const char = source[i]
    if (char === '*') {
      if (source[i + 1] === '*') { out += '.*'; i += 2; if (source[i] === '/') i++ }
      else { out += '[^/]*'; i++ }
    } else {
      out += char.replace(/[.+?^${}()|[\]\\]/g, '\\$&'); i++
    }
  }
  return new RegExp(out + '$')
}

export function scopeAllows(scopes, relPath) {
  const target = norm(relPath)
  return scopes.some(glob => globToRegExp(glob).test(target))
}

/**
 * Conservative overlap check between two scope lists. Glob intersection is
 * undecidable in general; we reject the decidable cases (equality, literal
 * matched by the other's glob, `**`-prefix containment) and document the rest.
 */
export function scopesOverlap(a, b) {
  for (const first of a) for (const second of b) {
    if (norm(first) === norm(second)) return true
    if (!/[*]/.test(second) && scopeAllows([first], second)) return true
    if (!/[*]/.test(first) && scopeAllows([second], first)) return true
    const base = value => norm(value).replace(/\/\*\*$/, '')
    const [ba, bb] = [base(first), base(second)]
    const starry = value => /\/\*\*$/.test(norm(value))
    if (starry(first) && (bb === ba || bb.startsWith(ba + '/'))) return true
    if (starry(second) && (ba === bb || ba.startsWith(bb + '/'))) return true
  }
  return false
}

function targetOf(exec) {
  const args = exec?.args ?? exec?.arguments
  if (args && typeof args === 'object') return args.file_path ?? args.path ?? null
  if (typeof args === 'string') {
    try { return JSON.parse(args)?.file_path ?? null } catch { return null }
  }
  return null
}

/**
 * Per-run write-scope claims for parallel implementer workers. In-memory during
 * a run; every claim is also appended to the audit ledger so diagnostics and a
 * cold read can reconstruct it. The record shape intentionally leaves room for
 * per-artifact state/version fields (a later v3 adds staged handoffs on top).
 */
export class WriteScopeRegistry {
  constructor({ journal } = {}) {
    this.journal = journal || null
    this.bySession = new Map()
    this.byRoot = new Map()
  }
  async claim({ rootId, sessionId, subtask, scopes, runId }) {
    if (!Array.isArray(scopes) || !scopes.length || scopes.some(s => typeof s !== 'string' || !s.trim())) {
      throw failure('WORKER_SCOPE_INVALID', 'Each parallel subtask needs a nonempty write_scope glob list')
    }
    const record = { root_session_id: rootId, worker_session_id: sessionId, subtask,
      scopes: scopes.map(norm), run_id: runId || null, claimed_at: Date.now() }
    // A subtask's region passes to its rework continuation: the prior claimant is
    // provably terminal before rework starts, so replace rather than overlap.
    const prior = this.claimsFor(rootId).find(row => row.subtask === subtask)
    if (prior) {
      this.bySession.delete(prior.worker_session_id)
      this.byRoot.get(rootId)?.delete(prior.worker_session_id)
    }
    const siblings = this.claimsFor(rootId).filter(row => row.worker_session_id !== sessionId)
    for (const sibling of siblings) {
      if (scopesOverlap(record.scopes, sibling.scopes)) {
        throw failure('WORKER_SCOPE_OVERLAP', `write_scope of ${subtask} overlaps with ${sibling.subtask}`)
      }
    }
    if (this.journal) await this.journal.append(rootId, 'dpswarm/write-scope', record)
    this.bySession.set(sessionId, record)
    if (!this.byRoot.has(rootId)) this.byRoot.set(rootId, new Map())
    this.byRoot.get(rootId).set(sessionId, record)
    return record
  }
  forSession(sessionId) { return this.bySession.get(sessionId) || null }
  claimsFor(rootId) { return [...(this.byRoot.get(rootId)?.values() || [])] }
  clear(rootId) {
    for (const sessionId of this.byRoot.get(rootId)?.keys() || []) this.bySession.delete(sessionId)
    this.byRoot.delete(rootId)
  }
}

/**
 * Enforce claims at the worker's tool entry. Reads are never gated; shell and
 * other free-form tools are out of scope for v2 (role guidance says no shell
 * writes in parallel mode; the audit trail records actual write/edit calls).
 */
export function installWriteScope(ctx, registry) {
  return ctx.on('tools/pre-execute', async (exec, next) => {
    const session = exec?.agent?.session
    const claim = session && registry.forSession(session.id)
    if (!claim || !GATED_TOOLS.has(exec.name)) return next()
    const cwd = session.header?.cwd
    if (typeof cwd !== 'string' || !cwd) {
      throw failure('WORKER_SCOPE_ENVELOPE_UNAVAILABLE', 'Cannot verify the write target without the session workspace')
    }
    const target = targetOf(exec)
    if (target === null) {
      throw failure('WORKER_SCOPE_TARGET_REQUIRED', `Scoped worker ${claim.subtask} must name a file_path for ${exec.name}`)
    }
    const rel = scopeRelativePath(cwd, target)
    if (rel === null || !scopeAllows(claim.scopes, rel)) {
      throw failure('WORKER_SCOPE_VIOLATION', `${claim.subtask} may only write ${claim.scopes.join(', ')}; got ${target}`)
    }
    return next()
  }, { prepend: true, global: true })
}
