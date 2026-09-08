import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { existsSync, mkdtempSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { createInterface } from 'node:readline'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import { FixedTeamController } from '../lib/fixed-team.js'
import { Sidecar } from '../lib/sidecar.js'

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
test('real session sidecar: fixed roles, route facts, project lease, two sessions, review and restart', { timeout: 30000 }, async t => {
  const directory = mkdtempSync(join(tmpdir(), 'dpswarm-integration-')), cwd = join(directory, 'project')
  mkdirSync(cwd)
  const child = spawn(process.env.DPSWARM_TEST_PYTHON || 'python', [join(repo, 'dpswarm-dsh-plugin/tests/session-sidecar-harness.py'), directory],
    { cwd: repo, windowsHide: true, shell: false, stdio: ['pipe','pipe','pipe'] })
  let stderr = '';child.stderr.on('data', b => { stderr += b })
  const lines = createInterface({ input: child.stdout }), queue = [], waiters = []
  lines.on('line', l => { const v = JSON.parse(l);if (waiters.length) waiters.shift()(v);else queue.push(v) })
  const next = () => queue.length ? Promise.resolve(queue.shift()) : new Promise(r => waiters.push(r))
  t.after(async () => { if (child.exitCode === null) { const done=once(child,'exit');child.stdin.end('{"command":"stop"}\n');await done } lines.close() })
  const { port } = await Promise.race([next(),once(child,'exit').then(()=>{throw new Error(stderr)})])
  const cfg = { sidecarUrl: `http://127.0.0.1:${port}`, workspace: directory, autoStart: false,
    enabledSessions: ['session-a','session-b'], subagentProvider: 'spawn', implProvider: 'fixture-provider', implModel: 'glm-5.3-flash',
    testProvider: 'fixture-provider', testModel: 'glm-5.3-flash', workerTimeoutSeconds: 600 }
  const starts = [], disposals = []
  const subagents = { async start(provider, request) {
    assert.equal(starts.length,disposals.length,'previous role disposed before next start')
    const id = 'actual-child-' + starts.length;starts.push(request)
    return { id, result: Promise.resolve({ output: [{ type: 'text', text: `offline fixture ${id}; no model call or file change` }], stopReason:'completed' }),
      async dispose() { disposals.push(id) } }
  } }
  const controller = new FixedTeamController({ config:()=>cfg, subagents })
  const makeExec = id => ({ agent: { id, session: { id, header: { cwd }, route: { provider:'fixture-lead', model:'gpt-5.6-sol' }, requestHeader() { return { config: this.route } } }, options: { provider:'startup', model:'deepseek-v4-pro', reasoningEffort:'high' } }, signal:new AbortController().signal })
  const a=makeExec('session-a'), b=makeExec('session-b')
  assert.equal((await controller.status(a.agent)).state,'not_started')
  const result=await controller.run({task:'offline contract verification'},a)
  assert.deepEqual(result.failed,[])
  assert.deepEqual(result.deliveries.map(d=>d.role),['implementer','tester'])
  assert.deepEqual(result.deliveries.map(d=>d.execution_session_id),disposals)
  assert.equal(result.profile.implementer.model,'glm-5.3-flash')
  assert.equal(result.deliveries[0].token_usage.input_tokens,null)
  const lease=controller.sessions.get('session-a').lease.path, leaseData=JSON.parse(readFileSync(lease,'utf8'))
  await assert.rejects(controller.run({task:'overlap'},b),/WORKSPACE_BUSY/)
  assert.equal(starts.length,2)
  child.stdin.write('{"command":"restart"}\n');assert.equal((await next()).restarted,true)
  cfg.enabledSessions=[]
  assert.equal((await controller.status(a.agent)).snapshot.open_worker_slots_used,2)
  for(const item of result.deliveries) await controller.review({item_id:item.item_id,verdict:'accept',reason:'offline fixture verified'},a)
  assert.equal(existsSync(lease),false)
  const auditA=await controller.sessions.get('session-a').sidecar.call('GET','/api/events?limit=1000')
  assert.equal(auditA.events.filter(e=>e.payload?.accepted_by?.review_note==='offline fixture verified').length,2)
  // Simulate a crash after durable acceptance but before deleting the project lease.
  writeFileSync(lease,JSON.stringify({...leaseData,pid:2147483647}))
  const recovered=new FixedTeamController({config:()=>cfg,subagents})
  assert.equal(recovered.session(a.agent).lease.recovered,true)
  assert.equal((await recovered.review({item_id:result.deliveries[0].item_id,verdict:'accept'},a)).outcome,'already_accepted')
  assert.equal(existsSync(lease),false)
  cfg.enabledSessions=['session-b']
  Object.assign(cfg,{reviewerMode:'model',reviewerProvider:'fixture-provider',reviewerModel:'glm-5.3-flash'})
  const second=await controller.run({task:'next session'},b)
  assert.deepEqual(second.failed,[])
  assert.deepEqual(second.deliveries.map(d=>d.role),['implementer','tester','reviewer'])
  const statusA=await controller.status(a.agent), statusB=await controller.status(b.agent)
  assert.equal(statusA.snapshot.open_worker_slots_used,0)
  assert.equal(statusB.snapshot.open_worker_slots_used,3)
  for(const item of second.deliveries) await controller.review({item_id:item.item_id,verdict:'terminate',reason:'end fixture'},b)
  const auditB=await controller.sessions.get('session-b').sidecar.call('GET','/api/events?limit=1000')
  assert.equal(auditB.events.filter(e=>e.kind==='work_item_terminated' && e.payload.summary==='end fixture').length,3)
  cfg.enabledSessions=['session-c'];cfg.implMode='lead';cfg.reviewerMode='lead'
  const c=makeExec('session-c');c.agent.session.route.reasoningEffort='max'
  const inherited=await controller.run({task:'inherited conversation model contract'},c)
  assert.deepEqual(inherited.failed,[])
  assert.equal(inherited.profile.implementer.mode,'lead')
  assert.deepEqual(Object.fromEntries(Object.entries(starts[5].agentOptions)),{provider:'fixture-lead',model:'gpt-5.6-sol',reasoningEffort:'max'})
  const inheritedState=await controller.status(c.agent)
  const worker=Object.values(inheritedState.snapshot.nodes).find(n=>n.execution_session_id===inherited.deliveries[0].execution_session_id)
  assert.equal(worker.requested_provider,'fixture-lead');assert.equal(worker.requested_model,'gpt-5.6-sol')
  for(const d of inherited.deliveries)await controller.review({item_id:d.item_id,verdict:'accept'},c)

})

test('new bridge refuses legacy sidecar before any worker call', async () => {
  const sidecar=new Sidecar({sidecarUrl:'http://127.0.0.1:8791',sessionIsolation:true,autoStart:false})
  sidecar.call=async()=>({snapshot:{}})
  await assert.rejects(sidecar.ensure(),/SIDECAR_VERSION_MISMATCH/)
})
