import { createHash } from 'node:crypto'

const fail = (code, message) => Object.assign(new Error(`${code}: ${message}`), { code })
const MAX_BYTES = 8 * 1024 * 1024
const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key)
const pointerKey = key => String(key).replaceAll('~', '~0').replaceAll('/', '~1')
const kind = value => value === null ? 'null' : Array.isArray(value) ? 'array' : typeof value

export function decodeEvidence(bytes, encoding = 'auto') {
  let data = new Uint8Array(bytes), selected = encoding
  if (!['auto', 'utf-8', 'utf-16le', 'utf-16be'].includes(selected)) throw fail('EVIDENCE_ENCODING_INVALID', 'Use auto, utf-8, utf-16le or utf-16be.')
  if (selected === 'auto') {
    selected = data[0] === 0xff && data[1] === 0xfe ? 'utf-16le'
      : data[0] === 0xfe && data[1] === 0xff ? 'utf-16be' : 'utf-8'
  }
  let text
  try { text = new TextDecoder(selected, { fatal: true }).decode(data) }
  catch { throw fail('EVIDENCE_DECODE_FAILED', 'The bytes do not match the selected text encoding. Specify the actual encoding; no content was evaluated.') }
  try { return { encoding: selected, value: JSON.parse(text.replace(/^\uFEFF/, '')) } }
  catch { throw fail('EVIDENCE_JSON_INVALID', 'The decoded file is not a complete JSON document. A file existing or a successful process exit is not a parsed result.') }
}

function select(value, pointer) {
  if (pointer === '') return value
  if (!pointer.startsWith('/') || /~(?:[^01]|$)/.test(pointer)) throw fail('EVIDENCE_POINTER_INVALID', 'Use an RFC 6901 JSON pointer, escaping ~ as ~0 and / as ~1.')
  for (const part of pointer.slice(1).split('/').map(s => s.replaceAll('~1', '/').replaceAll('~0', '~'))) {
    if (value === null || typeof value !== 'object' || !own(value, part)) throw fail('EVIDENCE_POINTER_MISSING', 'The selected JSON member does not exist; inspect its parent keys.')
    value = value[part]
  }
  return value
}

function preview(value) {
  const type = kind(value)
  if (type === 'object' || type === 'array') {
    const serialized = JSON.stringify(value)
    if (serialized.length <= 700) return { type, value, complete: true }
    return { type, size: Object.keys(value).length, complete: false, next: 'Read this entry pointer to inspect its children.' }
  }
  if (type === 'string' && value.length > 700) return { type, value: value.slice(0, 700), chars: value.length, complete: false }
  return { type, value, complete: true }
}

/** Uses the same host filesystem service as read: never opens host paths directly. */
export async function readEvidence(ctx, args, exec) {
  const { file_path, pointer = '', offset = 0, limit = 20, expected_sha256, encoding = 'auto' } = args || {}
  if (typeof file_path !== 'string' || !file_path.trim() || file_path.length>4000 || typeof pointer !== 'string' || pointer.length>8000
    || !Number.isSafeInteger(offset) || offset < 0 || !Number.isSafeInteger(limit) || limit < 1 || limit > 40
    || (expected_sha256 !== undefined && (typeof expected_sha256 !== 'string' || !/^[a-f0-9]{64}$/i.test(expected_sha256)))) throw fail('EVIDENCE_REQUEST_INVALID', 'Supply file_path, optional JSON pointer, nonnegative offset and limit 1–40; pin later pages with expected_sha256.')
  const fs = ctx.fs
  if (!fs?.resolve || !fs?.readBytes || !fs?.stat) throw fail('EVIDENCE_FS_UNAVAILABLE', 'The host filesystem byte-reading capability is unavailable.')
  exec.signal?.throwIfAborted()
  const cwd = exec.agent?.session?.header?.cwd
  const target = await fs.resolve(file_path, { ...(cwd ? { cwd } : {}), signal: exec.signal })
  const before = await fs.stat(target, exec.signal)
  if (!before || before.type !== 'file') throw fail('EVIDENCE_NOT_FILE', 'The requested evidence is not an existing regular file.')
  if (before.size > MAX_BYTES) throw fail('EVIDENCE_TOO_LARGE', 'JSON evidence exceeds the 8 MiB read limit. Produce a bounded summary through an authorized tool.')
  const bytes = await fs.readBytes(target, exec.signal, MAX_BYTES)
  if (bytes.byteLength > MAX_BYTES) throw fail('EVIDENCE_TOO_LARGE', 'The filesystem returned evidence exceeding the byte limit.')
  const sha256 = createHash('sha256').update(bytes).digest('hex')
  if (expected_sha256 && expected_sha256.toLowerCase() !== sha256) throw fail('EVIDENCE_CHANGED', 'The file differs from the digest of the earlier evidence page. Reassess the new result before combining observations.')
  const decoded = decodeEvidence(bytes, encoding), selected = select(decoded.value, pointer)
  const type = kind(selected)
  let page
  if (type === 'string') {
    const count = Math.min(limit * 200, 8000)
    const value = selected.slice(offset, offset + count)
    page = { type, value, offset_unit: 'characters', total: selected.length, next_offset: offset + value.length < selected.length ? offset + value.length : null }
  } else if (type === 'array' || type === 'object') {
    const keys = Object.keys(selected), entries = []
    for (const key of keys.slice(offset, offset + limit)) {
      if (key.length>2000) throw fail('EVIDENCE_KEY_TOO_LONG','The JSON member name exceeds the bounded page limit; obtain a bounded summary through an authorized tool.')
      const entry = { key, pointer: pointer + '/' + pointerKey(key), ...preview(selected[key]) }
      if (entries.length && Buffer.byteLength(JSON.stringify([...entries, entry]),'utf8') > 16000) break
      entries.push(entry)
    }
    page = { type, entries, total: keys.length, offset_unit: 'entries', next_offset: offset + entries.length < keys.length ? offset + entries.length : null }
  } else page = { type, value: selected, total: 1, next_offset: null }
  return { path: target.displayPath, sha256, bytes: bytes.byteLength, encoding: decoded.encoding, pointer, offset, ...page,
    evidence_status: 'observed-data-not-a-verdict',
    next: 'Use entry pointers and next_offset; include expected_sha256 to keep pages bound to the same bytes. Judge the measurement method and candidate binding separately; decoded data is not acceptance.' }
}
