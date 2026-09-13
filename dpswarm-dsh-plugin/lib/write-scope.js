import { isAbsolute, relative, resolve, sep } from 'node:path'
import { safeCandidatePath, verifyCandidateSnapshot } from './candidate-snapshot.js'

const failure = (code, message) => Object.assign(new Error(`${code}: ${message}`), { code })

/** Tools whose file target is enforced against a worker's claimed write scope. */
const GATED_TOOLS = new Set(['write', 'edit'])
/** Content-leaking read tools gated by artifact read-side state (glob/list stay free: they reveal only paths). */
const READ_GATED_TOOLS = new Set(['read', 'grep', 'dpswarm_read_evidence'])

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
    try { const parsed = JSON.parse(args); return parsed?.file_path ?? parsed?.path ?? null } catch { return null }
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
    this.artifacts = new Map()
    this.consumed = new Map()
    this.sealedArtifacts = new Map()
  }
  async claim({ rootId, sessionId, subtask, scopes, runId }) {
    if (!Array.isArray(scopes) || !scopes.length || scopes.some(s => typeof s !== 'string' || !s.trim())) {
      throw failure('WORKER_SCOPE_INVALID', 'Each parallel subtask needs a nonempty write_scope glob list')
    }
    const record = { root_session_id: rootId, worker_session_id: sessionId, subtask,
      scopes: scopes.map(norm), run_id: runId || null, claimed_at: Date.now(),
      // v3 extension point: the artifact entity builds staged states on these.
      state: 'claimed', version: 1 }
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
  /** In-process artifact mirror (the control plane stays the durable authority). */
  registerArtifact(rootId, artifact) {
    if (!this.artifacts.has(rootId)) this.artifacts.set(rootId, new Map())
    this.artifacts.get(rootId).set(artifact.id, { ...artifact })
  }
  updateArtifactState(rootId, artifactId, state, version) {
    const artifact = this.artifacts.get(rootId)?.get(artifactId)
    if (artifact) {
      artifact.state = state
      if (Number.isSafeInteger(version)) artifact.version = version
      if (!['ready', 'frozen', 'done'].includes(state)) { delete artifact.ready_manifest; delete artifact.manifest_digest }
      else if (artifact.ready_manifest && artifact.manifest_digest === artifact.ready_manifest.manifest_digest) {
        // Publish the direct-view read seam only after the control-plane state
        // transition has succeeded and the caller updates this mirror.
        if (!this.sealedArtifacts.has(rootId)) this.sealedArtifacts.set(rootId, new Map())
        this.sealedArtifacts.get(rootId).set(artifactId + ':' + artifact.manifest_digest, {
          id: artifactId, state: 'ready', acceptance_contract: 'dpswarm-acceptance-v1',
          manifest_digest: artifact.manifest_digest, ready_manifest: structuredClone(artifact.ready_manifest),
        })
      }
    }
  }
  /** Seal runtime bytes before the authoritative ready transition. */
  async bindReadyManifest(rootId, artifactId, candidate) {
    const artifact = this.artifacts.get(rootId)?.get(artifactId)
    if (!artifact) throw failure('ARTIFACT_UNKNOWN', artifactId)
    if (!candidate?.view_path) throw failure('ARTIFACT_MANIFEST_REQUIRED', 'A ready artifact needs a readable snapshot view')
    const verified = await verifyCandidateSnapshot(candidate)
    if (!verified.ok) throw failure('ARTIFACT_MANIFEST_INVALID', verified.errors.join('; '))
    if (candidate.kind !== 'files') throw failure('ARTIFACT_MANIFEST_INVALID', 'A staged artifact requires a file candidate')
    artifact.acceptance_contract = 'dpswarm-acceptance-v1'
    artifact.ready_manifest = structuredClone(candidate)
    artifact.manifest_digest = candidate.manifest_digest
    if (this.journal) await this.journal.append(rootId, 'dpswarm/artifact-ready-manifest', {
      artifact_id: artifactId, manifest_digest: candidate.manifest_digest,
      candidate_files: candidate.candidate_files.map(({ content_base64, ...file }) => file), view_path: candidate.view_path,
    })
    return { artifact_id: artifactId, manifest_digest: candidate.manifest_digest, view_path: candidate.view_path }
  }
  async recordConsumption(rootId, sessionId, artifact, path) {
    if (!this.consumed.has(rootId)) this.consumed.set(rootId, new Map())
    const records = this.consumed.get(rootId)
    const id = [sessionId, artifact.id, artifact.manifest_digest].join(':')
    const prior = records.get(id)
    const record = prior || { worker_session_id: sessionId, artifact_id: artifact.id, manifest_digest: artifact.manifest_digest, paths: [] }
    if (record.paths.includes(path)) return record
    const next = { ...record, paths: [...record.paths, path].sort() }
    if (this.journal) await this.journal.append(rootId, 'dpswarm/artifact-consumed-manifest', next)
    records.set(id, next)
    return next
  }
  sealedArtifactsFor(rootId) { return [...(this.sealedArtifacts.get(rootId)?.values() || [])] }
  consumedManifestRefsFor(rootId, sessionId = null) {
    return [...(this.consumed.get(rootId)?.values() || [])].filter(row => !sessionId || row.worker_session_id === sessionId).map(row => structuredClone(row))
  }
  artifactsFor(rootId) { return [...(this.artifacts.get(rootId)?.values() || [])] }
  /** The staged artifact owning this path, if any. */
  artifactAt(rootId, relPath) {
    return this.artifactsFor(rootId).find(a => Array.isArray(a.write_globs) && scopeAllows(a.write_globs, relPath)) || null
  }
  clear(rootId) {
    for (const sessionId of this.byRoot.get(rootId)?.keys() || []) this.bySession.delete(sessionId)
    this.byRoot.delete(rootId)
    this.artifacts.delete(rootId)
    this.consumed.delete(rootId)
    this.sealedArtifacts.delete(rootId)
  }
}

/**
 * Enforce claims and staged snapshot reads at the worker's tool entry. Shell and
 * other free-form tools remain outside this hook (role guidance says no shell
 * writes in parallel mode; the audit trail records actual write/edit calls).
 */
export function installWriteScope(ctx, registry) {
  return ctx.on('tools/pre-execute', async (exec, next) => {
    const session = exec?.agent?.session
    const claim = session && registry.forSession(session.id)
    if (!claim) return next()
    if (READ_GATED_TOOLS.has(exec.name)) {
      const strict = registry.artifactsFor(claim.root_session_id).filter(a => a.acceptance_contract === 'dpswarm-acceptance-v1')
      const cwd = session.header?.cwd
      const target = targetOf(exec)
      if (typeof cwd !== 'string' || !cwd || target === null) {
        if (strict.length) throw failure('ARTIFACT_SNAPSHOT_TARGET_REQUIRED', 'A strict staged read must identify its workspace and concrete file')
        return next()
      }
      const pendingView = strict.find(a => a.ready_manifest?.view_path && !['ready', 'frozen', 'done'].includes(a.state)
        && scopeRelativePath(a.ready_manifest.view_path, resolve(cwd, target)) !== null
        && !registry.sealedArtifactsFor(claim.root_session_id).some(sealed => sealed.manifest_digest === a.manifest_digest))
      if (pendingView) throw failure('ARTIFACT_NOT_READY', 'This snapshot has not completed its authoritative ready transition')
      const direct = registry.sealedArtifactsFor(claim.root_session_id).map(artifact => ({ artifact,
        path: scopeRelativePath(artifact.ready_manifest.view_path, resolve(cwd, target)),
      })).find(row => row.path !== null)
      const rel = direct?.path ?? scopeRelativePath(cwd, target)
      if (rel === null) return next()
      if (strict.length && rel && !direct) await safeCandidatePath(cwd, target, { allowMissing: true })
      const owner = direct?.artifact || registry.artifactAt(claim.root_session_id, rel)
      const upstream = strict.filter(a => a.id !== claim.subtask && a.ready_manifest?.candidate_files.some(file => norm(file.path) === rel))
      // An unchanged dependency still belongs to the input snapshot. Never infer
      // its runtime version from write_globs alone.
      const digests = new Set(upstream.map(a => a.manifest_digest))
      if (!owner && digests.size > 1) throw failure('ARTIFACT_DEPENDENCY_AMBIGUOUS', 'Several ready artifacts seal this dependency; read the intended snapshot view explicitly')
      const artifact = owner || upstream[0] || null
      if (artifact && artifact.id !== claim.subtask && !['ready', 'frozen', 'done'].includes(artifact.state || 'pending')) {
        throw failure('ARTIFACT_NOT_READY', `artifact ${artifact.id} is "${artifact.state || 'pending'}"; work your own scope first or end the turn and wait for wakeup`)
      }
      // Directory grep would leak mutable siblings. Strict reads name one sealed file.
      if (strict.length && exec.name === 'grep' && (!artifact || /[*?]/.test(target) || rel === '')) {
        throw failure('ARTIFACT_SNAPSHOT_TARGET_REQUIRED', 'In a strict staged run, grep must target one concrete sealed artifact file')
      }
      if (artifact?.acceptance_contract === 'dpswarm-acceptance-v1' && artifact.id !== claim.subtask) {
        const candidate = artifact.ready_manifest
        if (!candidate || artifact.manifest_digest !== candidate.manifest_digest) throw failure('ARTIFACT_MANIFEST_REQUIRED', artifact.id + ' has no matching ready snapshot')
        const verified = await verifyCandidateSnapshot(candidate)
        if (!verified.ok) throw failure('ARTIFACT_MANIFEST_INVALID', verified.errors.join('; '))
        const sealed = candidate.candidate_files.find(file => norm(file.path) === rel && file.operation === 'file')
        if (!sealed) throw failure('ARTIFACT_PATH_NOT_SEALED', rel + ' is absent from the ready snapshot')
        const snapshot = await safeCandidatePath(candidate.view_path, sealed.path)
        const slot = exec.args !== undefined ? 'args' : 'arguments'
        const original = exec[slot]
        let args
        try { args = typeof original === 'string' ? JSON.parse(original) : original } catch { throw failure('ARTIFACT_SNAPSHOT_TARGET_REQUIRED', 'Cannot rewrite the read target') }
        const updated = { ...args, ...(args.file_path !== undefined ? { file_path: snapshot.absolute } : { path: snapshot.absolute }) }
        exec[slot] = typeof original === 'string' ? JSON.stringify(updated) : updated
        try {
          const result = await next()
          const blocks = result?.message?.content || result?.content || []
          const failedRead = result?.isError === true || Boolean(result?.error)
            || (Array.isArray(blocks) && blocks.some(block => block?.type === 'tool-result' && block.isError === true))
          if (failedRead) return result
          await registry.recordConsumption(claim.root_session_id, session.id, { id: artifact.id, manifest_digest: candidate.manifest_digest }, sealed.path)
          return result
        } finally { exec[slot] = original }
      }
      return next()
    }
    if (!GATED_TOOLS.has(exec.name)) return next()
    const cwd = session.header?.cwd
    if (typeof cwd !== 'string' || !cwd) {
      throw failure('WORKER_SCOPE_ENVELOPE_UNAVAILABLE', 'Cannot verify the write target without the session workspace')
    }
    const target = targetOf(exec)
    if (target === null) {
      throw failure('WORKER_SCOPE_TARGET_REQUIRED', `Scoped worker ${claim.subtask} must name a file_path for ${exec.name}`)
    }
    const views = [...registry.sealedArtifactsFor(claim.root_session_id), ...registry.artifactsFor(claim.root_session_id).filter(a => a.ready_manifest?.view_path)]
    if (views.some(a => scopeRelativePath(a.ready_manifest.view_path, resolve(cwd, target)) !== null)) {
      throw failure('ARTIFACT_SNAPSHOT_IMMUTABLE', 'Managed tools cannot change a published snapshot view')
    }
    const rel = scopeRelativePath(cwd, target)
    if (rel === null || !scopeAllows(claim.scopes, rel)) {
      throw failure('WORKER_SCOPE_VIOLATION', `${claim.subtask} may only write ${claim.scopes.join(', ')}; got ${target}`)
    }
    const artifact = registry.artifactAt(claim.root_session_id, rel)
    if (artifact?.acceptance_contract === 'dpswarm-acceptance-v1') {
      await safeCandidatePath(cwd, target, { allowMissing: true })
      if (['ready', 'frozen', 'done'].includes(artifact.state)) {
        throw failure('ARTIFACT_READY_IMMUTABLE', artifact.id + ' is bound to its ready snapshot; move to draft and publish a new manifest before further writes')
      }
    }
    return next()
  }, { prepend: true, global: true })
}
