import test from 'node:test'
import assert from 'node:assert/strict'
import { decodeEvidence, readEvidence } from '../lib/evidence-reader.js'

function fixture(bytes, hooks = {}) {
  const calls = []
  const ctx = { fs: {
    async resolve(path, opts) { calls.push(['resolve', path, opts.cwd]); return { displayPath: path } },
    async stat() { return { type: 'file', size: bytes.length } },
    async readBytes(target, signal, cap) { calls.push(['read', cap]); return bytes }, ...hooks,
  } }
  return { ctx, calls, exec: { agent: { session: { header: { cwd: '/workspace' } } } } }
}
const body = { meta: { frames: 10 }, R: { seam: { 'h0/h2400~upper': { pixels: 0 } } } }

test('UTF16LE PowerShell output is decoded and selected in one observation', async () => {
  const f = fixture(Buffer.concat([Buffer.from([255,254]), Buffer.from(JSON.stringify(body), 'utf16le')]))
  const result = await readEvidence(f.ctx, { file_path: 'analysis.json', pointer: '/R/seam/h0~1h2400~0upper/pixels' }, f.exec)
  assert.equal(result.value, 0); assert.equal(result.encoding, 'utf-16le')
  assert.deepEqual(f.calls[0], ['resolve', 'analysis.json', '/workspace'])
  assert.equal(result.evidence_status, 'observed-data-not-a-verdict')
})
test('UTF8 BOM and UTF16BE decode, invalid bytes and partial JSON fail without evaluation', () => {
  assert.deepEqual(decodeEvidence(Buffer.from('\ufeff'+JSON.stringify(body))).value, body)
  const be = Buffer.from(JSON.stringify(body),'utf16le').swap16()
  assert.deepEqual(decodeEvidence(Buffer.concat([Buffer.from([254,255]),be])).value,body)
  assert.throws(()=>decodeEvidence(Buffer.from([255,0])), {code:'EVIDENCE_DECODE_FAILED'})
  assert.throws(()=>decodeEvidence(Buffer.from('{"value":')), {code:'EVIDENCE_JSON_INVALID'})
})
test('pages are bounded, complete JSON, digest pinned and explicit about missing members', async () => {
  const data = Object.fromEntries(Array.from({length:100},(_,i)=>[String(i),{payload:'x'.repeat(2000)}]))
  const f=fixture(Buffer.from(JSON.stringify(data)))
  const a=await readEvidence(f.ctx,{file_path:'a.json',limit:2},f.exec)
  assert.equal(a.entries.length,2);assert.equal(a.next_offset,2);assert.equal(a.entries[0].complete,false)
  assert.ok(JSON.stringify(a).length<16000)
  const b=await readEvidence(f.ctx,{file_path:'a.json',offset:2,limit:2,expected_sha256:a.sha256},f.exec)
  assert.equal(b.entries[0].key,'2')
  await assert.rejects(readEvidence(f.ctx,{file_path:'a.json',expected_sha256:'0'.repeat(64)},f.exec),{code:'EVIDENCE_CHANGED'})
  await assert.rejects(readEvidence(f.ctx,{file_path:'a.json',pointer:'/toString'},f.exec),{code:'EVIDENCE_POINTER_MISSING'})
  await assert.rejects(readEvidence(f.ctx,{file_path:'a.json',pointer:'/bad~2'},f.exec),{code:'EVIDENCE_POINTER_INVALID'})
})
test('filesystem denial, missing files, byte limits and cancellation are not bypassed', async () => {
  let read=false
  const denied=fixture(Buffer.from('{}'),{async resolve(){throw Object.assign(new Error('denied'),{code:'FS_DENIED'})},async readBytes(){read=true}})
  await assert.rejects(readEvidence(denied.ctx,{file_path:'outside'},denied.exec),{code:'FS_DENIED'});assert.equal(read,false)
  const big=fixture(Buffer.from('{}'),{async stat(){return {type:'file',size:9*1024*1024}}})
  await assert.rejects(readEvidence(big.ctx,{file_path:'big'},big.exec),{code:'EVIDENCE_TOO_LARGE'})
  const missing=fixture(Buffer.from('{}'),{async stat(){return undefined}})
  await assert.rejects(readEvidence(missing.ctx,{file_path:'missing'},missing.exec),{code:'EVIDENCE_NOT_FILE'})
  const abort=new AbortController();abort.abort()
  await assert.rejects(readEvidence(big.ctx,{file_path:'big'},{...big.exec,signal:abort.signal}))
})
