import { createHash, randomUUID } from 'node:crypto'
import { chmod, lstat, mkdir, readFile, readdir, realpath, rename, writeFile } from 'node:fs/promises'
import { dirname, extname, isAbsolute, join, relative, resolve, sep, win32 } from 'node:path'

export const CANDIDATE_SCHEMA = 'dpswarm-candidate-v1'
export const CANDIDATE_LIMITS = Object.freeze({ maxFiles: 64, maxBytes: 3 * 1024 * 1024, maxFileBytes: 3 * 1024 * 1024, maxRequestBytes: 5_000_000, maxDependencies: 256 })
const failure = (code, message) => Object.assign(new Error(`${code}: ${message}`), { code })
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex')
// First-version dependency closure supports web assets, not language/build packaging.
const UNSUPPORTED_EXECUTABLE_ENTRIES = new Set(['.py', '.pyw', '.ps1', '.psm1', '.sh', '.bash', '.zsh', '.bat', '.cmd', '.exe', '.com', '.jar', '.class', '.java', '.go', '.rs', '.c', '.cc', '.cpp', '.cxx', '.rb', '.php', '.pl', '.lua', '.r', '.jl', '.swift', '.kt', '.kts', '.scala', '.fs', '.fsx', '.wasm', '.ipynb'])
const key = path => path.replace(/\\/g, '/').toLowerCase()
const inside = (root, path) => {
  const rel = relative(root, path)
  return rel === '' || !(rel === '..' || rel.startsWith('..' + sep) || isAbsolute(rel))
}

export function canonicalJson(value) {
  if (Array.isArray(value)) return '[' + value.map(canonicalJson).join(',') + ']'
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(k => JSON.stringify(k) + ':' + canonicalJson(value[k])).join(',') + '}'
  return JSON.stringify(value)
}

export function candidateManifestMetadata(candidate) {
  return {
    schema: candidate.schema, kind: candidate.kind, binding: candidate.binding,
    entry_paths: candidate.entry_paths, changed_paths: candidate.changed_paths,
    candidate_files: candidate.candidate_files.map(({ content_base64, ...file }) => file),
    unknown_dependencies: candidate.unknown_dependencies, dependency_complete: candidate.dependency_complete,
    consumed_manifest_refs: candidate.consumed_manifest_refs,
  }
}

export function candidateManifestDigest(candidate) { return sha256(canonicalJson(candidateManifestMetadata(candidate))) }

function validRelativePath(path) {
  return typeof path === 'string' && path.length > 0 && path.length <= 512 && !/[\x00-\x1f<>"|?*]/.test(path)
    && !isAbsolute(path) && !win32.isAbsolute(path) && !path.includes('\\')
    && !path.split('/').some(part => !part || part === '.' || part === '..' || /[:]/.test(part) || /[. ]$/.test(part) || /^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(part))
}

/** Resolve within cwd and reject all symlink/junction ancestors, including aliases back into cwd. */
export async function safeCandidatePath(cwd, target, { allowMissing = false } = {}) {
  if (typeof target !== 'string' || !target.trim() || target.includes('\0')) throw failure('CANDIDATE_PATH_INVALID', 'Expected a nonempty path')
  const root = await realpath(resolve(cwd))
  const absolute = resolve(root, target.replace(/\\/g, '/'))
  if (!inside(root, absolute) || absolute === root) throw failure('CANDIDATE_PATH_ESCAPE', `Path is outside the workspace: ${target}`)
  const rel = relative(root, absolute).split(sep).join('/')
  if (!validRelativePath(rel)) throw failure('CANDIDATE_PATH_INVALID', `Unsupported or ambiguous path: ${target}`)
  let current = root
  for (const part of rel.split('/')) {
    current = join(current, part)
    let stat
    try { stat = await lstat(current) }
    catch (error) {
      if (allowMissing && error.code === 'ENOENT') return { root, absolute, path: process.platform === 'win32' ? rel.toLowerCase() : rel, missing: true }
      throw error
    }
    if (stat.isSymbolicLink()) throw failure('CANDIDATE_LINK_UNSUPPORTED', `Symlink/junction is not a sealed local dependency: ${target}`)
    const actual = await realpath(current)
    if (!inside(root, actual)) throw failure('CANDIDATE_PATH_ESCAPE', `Resolved path leaves the workspace: ${target}`)
  }
  const actual = await realpath(absolute)
  const path = relative(root, actual).split(sep).join('/')
  return { root, absolute: actual, path: process.platform === 'win32' ? path.toLowerCase() : path, missing: false }
}

function dependencies(path, source) {
  const references = []
  const unknown = []
  const add = value => { if (typeof value === 'string') references.push(value.trim()) }
  const css = code => {
    for (const match of code.matchAll(/url\(\s*(?:"([^"]*)"|'([^']*)'|([^\s)'";]+))\s*\)/gi)) add(match[1] ?? match[2] ?? match[3])
    for (const match of code.matchAll(/@import\s+['"]([^'"]+)['"]/gi)) add(match[1])
    if (/url\([^)]*(?:var\(|\$\{|\+)|@import\s+(?!url\(|['"])/i.test(code)) unknown.push({ reference: 'CSS dependency expression', reason: 'dynamic_dependency' })
  }
  const js = code => {
    for (const match of code.matchAll(/\b(?:import|export)\s+(?:[^;\n]*?\sfrom\s*)?['"]([^'"]+)['"]/g)) add(match[1])
    for (const match of code.matchAll(/\b(?:import|require|fetch|importScripts)\s*\(\s*(['"])([^'"\n]*)\1\s*\)/g)) add(match[2])
    for (const match of code.matchAll(/\bnew\s+URL\s*\(\s*(['"])([^'"\n]*)\1\s*,\s*import\.meta\.url\s*\)/g)) add(match[2])
    if (/\b(?:import|require|fetch|importScripts)\s*\(\s*(?!['"])[^\s)]|\b(?:import|require|fetch|importScripts)\s*\(\s*(['"])[^'"\n]*\1\s*[^\s)]/.test(code)
      || /\.(?:src|href)\s*=|\b(?:Worker|SharedWorker|XMLHttpRequest|WebSocket)\s*\(|setAttribute\s*\(\s*['"](?:src|href)['"]|document\.write\s*\(/.test(code)) {
      unknown.push({ reference: 'JavaScript runtime resource access', reason: 'dynamic_dependency' })
    }
    if (/\b(?:eval|Function)\s*\(|\[\s*['"](?:fetch|importScripts|src|href)['"]\s*\]|createElement\s*\(\s*['"](?:script|link|iframe|img|audio|video)['"]/.test(code)) {
      unknown.push({ reference: 'JavaScript constructed resource loader', reason: 'dynamic_dependency' })
    }
  }
  const suffix = extname(path).toLowerCase()
  if (['.html', '.htm', '.svg'].includes(suffix)) {
    for (const match of source.matchAll(/<([a-z][\w:-]*)\b([^>]*)>/gi)) {
      const tag = match[1].toLowerCase()
      if (tag === 'base') unknown.push({ reference: '<base>', reason: 'base_url_unsupported' })
      const attrs = match[2]
      for (const event of attrs.matchAll(/\bon[a-z]+\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/gi)) js(event[1] ?? event[2] ?? event[3])
      for (const attr of attrs.matchAll(/\b(src|href|xlink:href|poster|data|srcset|style)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/gi)) {
        const name = attr[1].toLowerCase(), value = attr[2] ?? attr[3] ?? attr[4]
        if (name === 'style') css(value)
        else if (name === 'srcset') for (const entry of value.split(',')) add(entry.trim().split(/\s+/)[0])
        else if ((name === 'href' || name === 'xlink:href') ? !['a', 'base'].includes(tag) : name === 'data' ? tag === 'object' : true) add(value)
      }
    }
    for (const match of source.matchAll(/<style\b[^>]*>([\s\S]*?)<\/style\s*>/gi)) css(match[1])
    for (const match of source.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script\s*>/gi)) js(match[1])
  } else if (suffix === '.css') css(source)
  else if (['.js', '.mjs', '.cjs', '.jsx', '.ts', '.tsx'].includes(suffix)) js(source)
  return { references, unknown }
}

function localDependency(owner, reference) {
  if (!reference || reference.startsWith('#') || /^data:/i.test(reference)) return { ignore: true }
  if (/^(?:[a-z][a-z\d+.-]*:|\/\/)/i.test(reference)) return { unknown: 'external_dependency' }
  if (/[${}<>]/.test(reference) || reference.includes('&')) return { unknown: 'dynamic_or_encoded_dependency' }
  let decoded
  try { decoded = decodeURIComponent(reference.split(/[?#]/)[0]) } catch { return { unknown: 'invalid_dependency_url' } }
  // Root-relative URLs require a server-root mapping, which a local view cannot infer.
  if (decoded.startsWith('/')) return { unknown: 'server_root_dependency' }
  if (['.js', '.mjs', '.cjs', '.jsx', '.ts', '.tsx'].includes(extname(owner).toLowerCase()) && !decoded.startsWith('.') && !decoded.includes('/')) return { unknown: 'unresolved_module_or_resource' }
  return { target: join(dirname(owner), decoded) }
}

function validateLimits(limits) {
  const out = { ...CANDIDATE_LIMITS, ...limits }
  for (const [name, value] of Object.entries(out)) if (!Number.isSafeInteger(value) || value < 1 || value > CANDIDATE_LIMITS[name]) throw failure('CANDIDATE_LIMIT_INVALID', `Invalid ${name}; cannot exceed the protocol limit`)
  return out
}

/** Runtime file collection; changed_paths never substitutes for the dependency closure. */
export async function collectCandidateSnapshot({ cwd, entryPaths = [], changedPaths = [], text = null, storageDir, binding = {}, consumedManifestRefs = [], limits = {} } = {}) {
  if (typeof cwd !== 'string' || !cwd) throw failure('CANDIDATE_CWD_REQUIRED', 'A workspace is required')
  if (!Array.isArray(entryPaths) || !Array.isArray(changedPaths) || !Array.isArray(consumedManifestRefs)) throw failure('CANDIDATE_INPUT_INVALID', 'Paths and consumed references must be arrays')
  changedPaths = [...new Set(changedPaths)]
  const bound = validateLimits(limits)
  if (entryPaths.length > bound.maxFiles || changedPaths.length > bound.maxFiles) throw failure('CANDIDATE_PATH_COUNT_LIMIT', 'Entry and changed path lists each support at most ' + bound.maxFiles + ' paths')
  const files = [], unknown = [], entries = [], changed = []
  const seen = new Set(), requested = new Map(), queue = []
  let totalBytes = 0, edges = 0
  const unknownKeys = new Set()
  const addUnknown = (path, reference, reason) => {
    const id = JSON.stringify([path, reference, reason])
    if (unknownKeys.has(id)) return
    if (unknown.length >= bound.maxDependencies - 1) {
      if (unknown.length < bound.maxDependencies) unknown.push({ path: '*', reference: 'Additional dependency references were not sealed', reason: 'dependency_count_limit' })
      return
    }
    unknownKeys.add(id)
    unknown.push({ path, reference, reason })
  }
  const addInput = async (target, list, kind) => {
    const info = await safeCandidatePath(cwd, target, { allowMissing: true })
    const id = key(info.path)
    if (list.some(path => key(path) === id)) {
      if (kind === 'changed') return
      throw failure('CANDIDATE_DUPLICATE_PATH', `Duplicate ${kind} path: ${target}`)
    }
    const prior = requested.get(id)
    if (prior && prior !== info.path) throw failure('CANDIDATE_DUPLICATE_PATH', `Case-alias path: ${target}`)
    requested.set(id, info.path); list.push(info.path)
    queue.push({ ...info, isEntry: kind === 'entry', isChanged: kind === 'changed' })
  }
  if (text !== null) {
    if (typeof text !== 'string' || entryPaths.length || changedPaths.length) throw failure('CANDIDATE_KIND_CONFLICT', 'Text candidates cannot replace file deliveries')
    const bytes = Buffer.from(text)
    if (bytes.length > bound.maxBytes || bytes.length > bound.maxFileBytes) throw failure('CANDIDATE_SIZE_LIMIT', 'Text candidate exceeds the byte limit')
    files.push({ path: '__text__/delivery.txt', operation: 'file', sha256: sha256(bytes), size: bytes.length, content_base64: bytes.toString('base64') })
    entries.push('__text__/delivery.txt')
  } else {
    if (!entryPaths.length) throw failure('CANDIDATE_ENTRY_REQUIRED', 'File delivery requires explicit entry paths')
    for (const target of entryPaths) await addInput(target, entries, 'entry')
    for (const target of changedPaths) await addInput(target, changed, 'changed')
    while (queue.length) {
      const info = queue.shift(), id = key(info.path)
      if (seen.has(id)) continue
      seen.add(id)
      if (info.missing) {
        if (changed.includes(info.path) && !entries.includes(info.path)) files.push({ path: info.path, operation: 'deleted', sha256: null, size: 0 })
        else addUnknown(info.owner || info.path, info.reference || info.path, 'missing_dependency')
        continue
      }
      if (files.length >= bound.maxFiles) { addUnknown(info.path, info.path, 'file_count_limit'); continue }
      const before = await lstat(info.absolute)
      if (!before.isFile()) { addUnknown(info.path, info.path, 'not_a_regular_file'); continue }
      if (before.size > bound.maxFileBytes || totalBytes + before.size > bound.maxBytes) { addUnknown(info.path, info.path, 'byte_limit'); continue }
      // Re-resolve before and after reading: managed snapshots reject path replacement.
      await safeCandidatePath(cwd, info.path)
      const bytes = await readFile(info.absolute)
      const afterInfo = await safeCandidatePath(cwd, info.path), after = await lstat(afterInfo.absolute)
      if (before.dev !== after.dev || before.ino !== after.ino || before.size !== after.size || before.mtimeMs !== after.mtimeMs || bytes.length !== after.size) throw failure('CANDIDATE_CHANGED_DURING_READ', info.path)
      if (bytes.length > bound.maxFileBytes || totalBytes + bytes.length > bound.maxBytes) { addUnknown(info.path, info.path, 'byte_limit'); continue }
      totalBytes += bytes.length
      files.push({ path: info.path, operation: 'file', sha256: sha256(bytes), size: bytes.length, content_base64: bytes.toString('base64') })
      if (entries.includes(info.path) && UNSUPPORTED_EXECUTABLE_ENTRIES.has(extname(info.path).toLowerCase())) {
        addUnknown(info.path, info.path, 'unsupported_executable_entry_dependencies')
      }
      const found = dependencies(info.path, bytes.toString('utf8'))
      for (const row of found.unknown) addUnknown(info.path, row.reference, row.reason)
      for (const reference of new Set(found.references)) {
        if (++edges > bound.maxDependencies) { addUnknown(info.path, reference, 'dependency_count_limit'); continue }
        const dependency = localDependency(info.path, reference)
        if (dependency.ignore) continue
        if (dependency.unknown) { addUnknown(info.path, reference, dependency.unknown); continue }
        try {
          const local = await safeCandidatePath(cwd, dependency.target, { allowMissing: true })
          const prior = requested.get(key(local.path))
          if (prior && prior !== local.path) throw failure('CANDIDATE_DUPLICATE_PATH', local.path)
          requested.set(key(local.path), local.path)
          queue.push({ ...local, owner: info.path, reference })
        } catch (error) { addUnknown(info.path, reference, error.code || 'dependency_unavailable') }
      }
    }
  }
  files.sort((a, b) => a.path < b.path ? -1 : a.path > b.path ? 1 : 0)
  entries.sort(); changed.sort()
  unknown.sort((a, b) => canonicalJson(a).localeCompare(canonicalJson(b)))
  const candidate = { schema: CANDIDATE_SCHEMA, kind: text === null ? 'files' : 'text', binding: structuredClone(binding), entry_paths: entries, changed_paths: changed,
    candidate_files: files, unknown_dependencies: unknown, dependency_complete: unknown.length === 0,
    consumed_manifest_refs: structuredClone(consumedManifestRefs) }
  candidate.manifest_digest = candidateManifestDigest(candidate)
  if (Buffer.byteLength(JSON.stringify(candidate)) > bound.maxRequestBytes) throw failure('CANDIDATE_REQUEST_LIMIT', 'Encoded candidate exceeds the transport limit')
  candidate.view_path = await materializeCandidateSnapshot(candidate, storageDir || join(cwd, '.dpswarm', 'candidate-snapshots'))
  return candidate
}

/** Publish a content-addressed, read-only-by-convention view. Integrity is always rechecked. */
export async function materializeCandidateSnapshot(candidate, storageDir, { renameView = rename } = {}) {
  const initial = await verifyCandidateSnapshot(candidate)
  if (!initial.ok) throw failure('CANDIDATE_INVALID', initial.errors.join('; '))
  await mkdir(storageDir, { recursive: true })
  const root = await realpath(resolve(storageDir)), destination = join(root, candidate.manifest_digest), view = join(destination, 'view')
  try {
    await lstat(destination)
    const existing = await verifyCandidateSnapshot({ ...candidate, view_path: view })
    if (!existing.ok) throw failure('CANDIDATE_VIEW_CORRUPT', existing.errors.join('; '))
    return view
  } catch (error) { if (error.code !== 'ENOENT') throw error }
  const temporary = join(root, `.pending-${randomUUID()}`), tempView = join(temporary, 'view')
  await mkdir(tempView, { recursive: true })
  for (const file of candidate.candidate_files) {
    if (file.operation === 'deleted') continue
    const path = join(tempView, ...file.path.split('/'))
    await mkdir(dirname(path), { recursive: true })
    await writeFile(path, Buffer.from(file.content_base64, 'base64'), { flag: 'wx', mode: 0o444 })
    await chmod(path, 0o444)
  }
  await writeFile(join(temporary, 'manifest.json'), JSON.stringify({ ...candidate, view_path: view }, null, 2), { flag: 'wx', mode: 0o444 })
  for (let attempt = 0; attempt < 5; attempt++) {
    try { await renameView(temporary, destination); break }
    catch (error) {
      if (!['EEXIST', 'ENOTEMPTY', 'EPERM', 'EBUSY', 'EACCES'].includes(error.code)) throw error
      let exists = false
      try { await lstat(destination); exists = true } catch (statError) { if (statError.code !== 'ENOENT') throw statError }
      if (exists) {
        const existing = await verifyCandidateSnapshot({ ...candidate, view_path: view })
        if (!existing.ok) throw Object.assign(failure('CANDIDATE_VIEW_CORRUPT', existing.errors.join('; ')), { cause: error })
        // A concurrent publisher won. Its bytes were verified; retain our temp
        // as diagnostic evidence and never remove another writer's view.
        return view
      }
      if (attempt === 4) throw Object.assign(failure('CANDIDATE_PUBLISH_FAILED', error.code + ': snapshot directory was not published after 5 attempts'), { cause: error, temporary_path: temporary, destination })
      // Windows scanners can temporarily hold a just-written directory. Missing
      // destination is an unpublished snapshot, never proof a peer succeeded.
      await new Promise(resolve => setTimeout(resolve, 50 * (attempt + 1)))
    }
  }
  const published = await verifyCandidateSnapshot({ ...candidate, view_path: view })
  if (!published.ok) throw failure('CANDIDATE_VIEW_CORRUPT', published.errors.join('; '))
  return view
}

export async function verifyCandidateSnapshot(candidate, { cwd } = {}) {
  const errors = [], workspaceErrors = []
  if (!candidate || candidate.schema !== CANDIDATE_SCHEMA || !['files', 'text'].includes(candidate.kind) || !Array.isArray(candidate.candidate_files)) return { ok: false, errors: ['Invalid candidate schema'] }
  const ids = new Set()
  let total = 0
  for (const file of candidate.candidate_files) {
    if (!validRelativePath(file.path) || ids.has(key(file.path))) { errors.push(`Invalid or duplicate file path: ${file.path}`); continue }
    ids.add(key(file.path))
    if (!['file', 'deleted'].includes(file.operation)) { errors.push(`Invalid operation: ${file.path}`); continue }
    if (file.operation === 'file') {
      const bytes = Buffer.from(typeof file.content_base64 === 'string' ? file.content_base64 : '', 'base64')
      if (bytes.toString('base64') !== file.content_base64 || bytes.length !== file.size || sha256(bytes) !== file.sha256) errors.push(`Byte/hash mismatch: ${file.path}`)
      total += bytes.length
    } else if (file.size !== 0 || file.sha256 !== null || file.content_base64 !== undefined) errors.push(`Invalid deletion: ${file.path}`)
    if (candidate.view_path) {
      try {
        const safe = await safeCandidatePath(candidate.view_path, file.path, { allowMissing: file.operation === 'deleted' })
        if (file.operation === 'deleted') { if (!safe.missing) errors.push(`Deleted file present in view: ${file.path}`) }
        else if (sha256(await readFile(safe.absolute)) !== file.sha256) errors.push(`View hash mismatch: ${file.path}`)
      } catch (error) { errors.push(`View unavailable: ${file.path}: ${error.code || error.message}`) }
    }
    if (cwd && candidate.kind === 'files') {
      try {
        const safe = await safeCandidatePath(cwd, file.path, { allowMissing: file.operation === 'deleted' })
        if (file.operation === 'deleted' ? !safe.missing : sha256(await readFile(safe.absolute)) !== file.sha256) workspaceErrors.push(`Workspace differs: ${file.path}`)
      } catch { workspaceErrors.push(`Workspace unavailable: ${file.path}`) }
    }
  }
  if (candidate.candidate_files.length > CANDIDATE_LIMITS.maxFiles || total > CANDIDATE_LIMITS.maxBytes) errors.push('Candidate exceeds protocol limits')
  try { if (candidateManifestDigest(candidate) !== candidate.manifest_digest) errors.push('Manifest digest mismatch') } catch { errors.push('Invalid manifest metadata') }
  if (candidate.view_path) {
    try {
      const viewStat = await lstat(candidate.view_path)
      if (!viewStat.isDirectory() || viewStat.isSymbolicLink()) errors.push('View root must be a regular directory')
      if (key(await realpath(candidate.view_path)) !== key(resolve(candidate.view_path))) errors.push('View path contains a symlink or junction alias')
      const walk = async dir => {
        for (const entry of await readdir(dir, { withFileTypes: true })) {
          const path = join(dir, entry.name), rel = relative(candidate.view_path, path).split(sep).join('/')
          if (entry.isSymbolicLink()) errors.push(`View link: ${rel}`)
          else if (entry.isDirectory()) await walk(path)
          else if (!ids.has(key(rel))) errors.push(`Unsealed view file: ${rel}`)
        }
      }
      await walk(candidate.view_path)
    } catch (error) { errors.push(`View unavailable: ${error.code || error.message}`) }
  }
  return { ok: errors.length === 0, errors, ...(cwd ? { workspace_matches: workspaceErrors.length === 0, workspace_errors: workspaceErrors } : {}) }
}
