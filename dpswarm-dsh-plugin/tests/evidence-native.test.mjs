import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'
import { readEvidence } from '../lib/evidence-reader.js'
const host=resolveHostRoot()
const [{Context},{LocalFileSystem}]=await Promise.all(['cordis','dsh-fs-local'].map(n=>import(hostModuleUrl(host,n+'/lib/index.js'))))

test('real Cordis filesystem provider reads PowerShell UTF16 evidence through declared injection',async t=>{
  const cwd=mkdtempSync(join(tmpdir(),'dpswarm-evidence-')),root=new Context()
  const path=join(cwd,'analysis.json'),value={R:{seam:{pixels:0}},note:'云相位'}
  writeFileSync(path,Buffer.concat([Buffer.from([255,254]),Buffer.from(JSON.stringify(value),'utf16le')]))
  const provider=root.plugin({name:'evidence-local-fixture',apply(ctx){new LocalFileSystem(ctx,{cwd,diffBasisMaxBytes:1024*1024})}})
  await provider
  let readCtx
  const consumer=root.plugin({name:'evidence-consumer-fixture',apply(ctx){ctx.inject(['fs'],runtime=>{readCtx=runtime})}})
  await consumer
  t.after(async()=>{await consumer.dispose();await provider.dispose()})
  assert.ok(readCtx)
  const exec={agent:{session:{header:{cwd}}},signal:new AbortController().signal}
  const first=await readEvidence(readCtx,{file_path:'analysis.json',pointer:'/R/seam/pixels'},exec)
  assert.equal(first.value,0);assert.equal(first.encoding,'utf-16le')
  writeFileSync(path,JSON.stringify({R:{seam:{pixels:8}}}))
  await assert.rejects(readEvidence(readCtx,{file_path:'analysis.json',pointer:'/R/seam/pixels',expected_sha256:first.sha256},exec),{code:'EVIDENCE_CHANGED'})
})
