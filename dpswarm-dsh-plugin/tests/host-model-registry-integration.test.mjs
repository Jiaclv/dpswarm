import assert from 'node:assert/strict'
import test from 'node:test'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { createInterface } from 'node:readline'
import { FixedTeamController } from '../lib/fixed-team.js'
import { HostModelRegistry } from '../lib/host-model-registry.js'

test('new host-resolved model runs through real sidecar; cold review survives provider removal', {timeout:30000}, async t => {
  const directory=mkdtempSync(join(tmpdir(),'dpswarm-host-registry-')), cwd=join(directory,'project');mkdirSync(cwd)
  const child=spawn(process.env.DPSWARM_PYTHON || 'python',[resolve('dpswarm-dsh-plugin/tests/session-sidecar-harness.py'),directory],{windowsHide:true,shell:false,stdio:['pipe','pipe','pipe']})
  let stderr='';child.stderr.on('data',b=>{stderr+=b})
  const lines=createInterface({input:child.stdout}),queue=[],waiters=[]
  lines.on('line',line=>{const v=JSON.parse(line);if(waiters.length)waiters.shift()(v);else queue.push(v)})
  const next=()=>queue.length?Promise.resolve(queue.shift()):new Promise(r=>waiters.push(r))
  t.after(async()=>{if(child.exitCode===null){const ended=once(child,'exit');child.stdin.end('{"command":"stop"}\n');await ended}lines.close()})
  const {port}=await Promise.race([next(),once(child,'exit').then(()=>{throw new Error(stderr)})])
  const cfg={sidecarUrl:`http://127.0.0.1:${port}`,workspace:directory,autoStart:false,enabledSessions:['host-root'],subagentProvider:'spawn',
    implMode:'lead',implProvider:'',implModel:'',testProvider:'fixture-glm',testModel:'brand-new-tester',workerTimeoutSeconds:60}
  let available=true,resolves=0,starts=0
  const registry=new HostModelRegistry(()=>({async resolveCallConfig(input){resolves++;if(!available)throw new Error('provider removed');return input}}))
  const subagents={async start(_provider,request){starts++;return {id:'native-fixture-'+starts,result:Promise.resolve({output:[{type:'text',text:'offline fixture delivery'}],stopReason:'completed'}),async dispose(){}}}}
  const controller=new FixedTeamController({config:()=>cfg,subagents,modelRegistry:registry})
  const parent={id:'host-root',session:{id:'host-root',header:{cwd},requestHeader:()=>({config:{provider:'deepseek-official',model:'deepseek-v4.1-flash-expires-on-0910',reasoningEffort:'max'}})}}
  const exec={agent:parent,signal:new AbortController().signal}
  const result=await controller.run({task:'offline bridge contract; no model calls'},exec)
  assert.equal(starts,2);assert.deepEqual(result.failed,[]);assert.equal(result.deliveries.length,2)
  const state=controller.sessions.get(parent.id),health=await state.sidecar.call('GET','/api/status')
  assert.equal(health.bridge.host_catalog_v1,true)
  assert.equal(health.snapshot.open_worker_slots_used,2)
  const lease=state.lease.path, savedLease=JSON.parse(readFileSync(lease,'utf8'))
  // Withdrawal is authoritative for new admission but must not block settlement.
  await state.sidecar.call('POST','/api/models/host-catalog',{source:'dph',models:[]})
  available=false;const resolvedBeforeReview=resolves
  child.stdin.write('{"command":"restart"}\n');assert.equal((await next()).restarted,true)
  writeFileSync(lease,JSON.stringify({...savedLease,pid:2147483647}))
  const recovered=new FixedTeamController({config:()=>cfg,subagents,modelRegistry:registry})
  for(const item of result.deliveries) await recovered.review({item_id:item.item_id,verdict:'accept'},exec)
  assert.equal(resolves,resolvedBeforeReview);assert.equal(starts,2);assert.equal(existsSync(lease),false)
  assert.equal((await recovered.status(parent)).snapshot.open_worker_slots_used,0)
  await assert.rejects(recovered.run({task:'provider no longer exists'},exec),{code:'HOST_MODEL_UNAVAILABLE'})
  assert.equal(starts,2)
})
