import assert from 'node:assert/strict'
import test from 'node:test'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'

const host = resolveHostRoot()
await import(hostModuleUrl(host,'cosmokit/lib/index.js'))
await import(hostModuleUrl(host,'dsh-tools/lib/index.js'))
const { apply, Config } = await import('../lib/index.js')

test('real installed DSH tool/schema API accepts the default-off fixed plugin', async () => {
  process.env.DPSWARM_SKIP_SETTINGS='1'
  const registered=new Map(), effects=[], prompts=[]
  const ctx={on(){return ()=>{}},provide(name,value){this[name]=value},tools:{register(t){registered.set(t.name,t)}},subagents:{start(){throw new Error('Must not start')}},
    systemPrompt:{section(p){prompts.push(p)}},inject(_deps,fn){fn(ctx)},effect(fn){effects.push(fn)}}
  apply(ctx,{})
  assert.deepEqual([...registered.keys()].sort(),['dpswarm_artifact','dpswarm_mailbox','dpswarm_models','dpswarm_prepare_worker','dpswarm_report','dpswarm_review','dpswarm_rework','dpswarm_run','dpswarm_status'])
  assert.equal(registered.has('dpswarm_delegate'),false)
  for(const effect of effects) effect()
  const parent={id:'p',session:{id:'p',header:{}},options:{provider:'gpt',model:'sol'}}
  const result=await registered.get('dpswarm_status').execute({}, {agent:parent})
  assert.equal(result.enabled,false)
  await assert.rejects(registered.get('dpswarm_run').execute({task:'work'},{agent:parent}),/DPSWARM_DISABLED/)
  const runTool = registered.get('dpswarm_run')
  assert.ok(runTool.parameters.properties.subtasks && runTool.parameters.properties.staged, 'run declares both split forms in its schema (0.9.3)')
  assert.match(runTool.description,/team_mode/)
  assert.equal(result.team_mode.mode,'serial')
  assert.equal(result.team_mode.source,'default')
  const leadPrompt = prompts[0].text({ agent: parent })
  assert.match(leadPrompt,/off by default/)
  assert.match(leadPrompt,/fixed rework allowance \(600000 tokens \/ 28 calls per rework attempt\)/)
  const childPrompt = prompts[0].text({ agent: { session: { id: 'child', header: { origin: 'subagent', parentSession: 'p', delegationDepth: 1 } } } })
  assert.match(childPrompt,/DPSwarm worker: execute/)
  assert.doesNotMatch(childPrompt,/DPSwarm Lead: plan|call dpswarm_status once/)
  assert.equal(result.budget_planning.modifies_limits, false)
  assert.equal(result.budget_planning.envelope_reference, null)
  assert.ok(Config)
})

test('settings service is the authority and cannot be replaced by model arguments', async () => {
  delete process.env.DPSWARM_SKIP_SETTINGS
  const value={enabledSessions:[],sidecarUrl:'http://127.0.0.1:8791',subagentProvider:'spawn'}
  const tools=new Map()
  const ctx={on(){return ()=>{}},provide(name,value){this[name]=value},tools:{register(t){tools.set(t.name,t)}},subagents:{start(){throw new Error('no model')}},
    settings:{register(){return {get:()=>value,watch(){return ()=>{}}}}},
    systemPrompt:{section(){}},inject(_deps,fn){fn(ctx)},effect(){}}
  apply(ctx,{autoStart:false})
  const parent={id:'p',session:{id:'p',header:{}},options:{provider:'gpt',model:'sol'}}
  assert.equal((await tools.get('dpswarm_status').execute({}, {agent:parent})).enabled,false)
  await assert.rejects(tools.get('dpswarm_run').execute({task:'work',enabled:true},{agent:parent}),/DPSWARM_DISABLED/)
})


test('worker limits have independent modes and old settings stay unrestricted', () => {
  const old = Config({})
  assert.equal(old.workerBudgetMode, 'unlimited')
  assert.equal(old.workerTokenLimit, 1200000)
  assert.equal(old.workerCallLimit, 50)
  assert.deepEqual(old.workerBudgetSessionOverrides, [])
  assert.equal(old.reworkBudgetMode, 'fixed')
  assert.equal(old.reworkTokenLimit, 600000)
  assert.equal(old.reworkCallLimit, 28)
  const custom=Config({workerBudgetMode:'manual',workerTokenLimit:123456,workerCallLimit:17,
    reworkBudgetMode:'fixed',reworkTokenLimit:234567,reworkCallLimit:9,
    workerBudgetSessionOverrides:[{sessionId:'experiment-A',mode:'auto'},{sessionId:'experiment-B',mode:'manual',tokenLimit:246912,callLimit:34}]})
  assert.equal(custom.workerBudgetSessionOverrides[1].tokenLimit,246912)
  assert.equal(custom.workerBudgetSessionOverrides[0].mode,'auto')
  assert.equal(custom.reworkBudgetMode,'fixed')
  assert.equal(custom.reworkTokenLimit,234567)
  assert.throws(()=>Config({workerBudgetMode:'team-total'}))
  assert.throws(()=>Config({workerTokenLimit:0}))
  assert.throws(()=>Config({workerCallLimit:-1}))
  assert.throws(()=>Config({reworkBudgetMode:'auto'}))
  assert.throws(()=>Config({reworkTokenLimit:0}))
  assert.throws(()=>Config({reworkCallLimit:-1}))
})

test('models tool reports effective Lead selection instead of startup options', async () => {
  process.env.DPSWARM_SKIP_SETTINGS='1'
  const tools=new Map()
  const ctx={on(){return ()=>{}},provide(name,value){this[name]=value},tools:{register(t){tools.set(t.name,t)}},
    subagents:{start(){throw new Error('Must not start')}},systemPrompt:{section(){}},inject(_deps,fn){fn(ctx)},effect(){}}
  apply(ctx,{autoStart:false,testProvider:'glmcp',testModel:'glm-5.3-flash'})
  const parent={id:'p',session:{id:'p',header:{},requestHeader:()=>({config:{provider:'glmcp',model:'glm-5.3'}})},
    options:{provider:'deepseek-official',model:'deepseek-v4-pro',reasoningEffort:'high'}}
  const result=await tools.get('dpswarm_models').execute({}, {agent:parent})
  assert.deepEqual(result.configured_profile.implementer,{mode:'lead',provider:'glmcp',model:'glm-5.3'})
  assert.deepEqual(result.configured_profile.reviewer,{mode:'lead'})
})
