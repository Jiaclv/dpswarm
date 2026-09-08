/** Isolated native DSH AgentLoop probe; fixture adapter only, no model network. */
import assert from 'node:assert/strict'
import { writeFileSync } from 'node:fs'
import { randomUUID } from 'node:crypto'
import { AuditJournal } from '../lib/audit.js'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'
const host=resolveHostRoot()
const {LlmAdapter,createUserMessage}=await import(hostModuleUrl(host,'dsh-llm/lib/index.js'))
export const inject=['agents','agentPresets','llm','dpswarmBudget','settings','sessions','subagents']
export function apply(ctx){
 if(!process.env.DPSWARM_BUDGET_PROBE_OUTPUT)throw new Error('Output path required for isolated probe')
 const seen=[],results=[],handles=[]
 class Fixture extends LlmAdapter {
  async resolveModel(provider,id){return {provider,id,name:id,defaultMaxTokens:1024,reasoning:{efforts:[{id:'max',name:'Max'}]}}}
  async *stream(options){
   assert.equal(options.provider,'worker-budget-fixture')
   const planning=String(options.system||'').includes('estimating resources for one worker')
   assert.equal(planning,false,'No hidden planner may request a model response')
   seen.push({sid:options.sessionId,planning,purpose:options.purpose||'main',maxTokens:options.maxTokens,effort:options.reasoningEffort,messages:options.messages})
   const firstToolLead=options.sessionId.endsWith('-tool-lead') && seen.filter(r=>r.sid===options.sessionId).length===1
   if(firstToolLead) {
    assert.ok(options.tools.some(t=>t.name==='dpswarm_prepare_worker'),'real preset exposes the allocation tool')
    yield {type:'block-end',index:0,block:{type:'tool-call',id:'fixture-budget-decision',name:'dpswarm_prepare_worker',arguments:JSON.stringify({task:'Native spawn assigned by the actual Lead tool.',tokenLimit:80000,callLimit:3,reason:'The Lead read the assigned task; allow three small worker requests.'})}}
   } else yield {type:'block-end',index:0,block:{type:'text',text:'Completed fixture response; no files or tests.'}}
   yield {type:'usage',usage:{inputTokens:planning?200:100,cacheReadTokens:10,outputTokens:planning?40:20}}
   yield {type:'finish',reason:{kind:'stop'}}
  }
 }
 ctx.llm.registerAdapter(['worker-budget-fixture'],new Fixture())
 async function create(id,parent){
  const h=await ctx.agents.create({sessionId:id,meta:{cwd:process.cwd(),...(parent?{parentSession:parent.session.id,delegationDepth:1,origin:'subagent'}:{agentPreset:'standard'})},
   agentOptions:{provider:'worker-budget-fixture',model:parent?'worker-fixture':'lead-fixture',reasoningEffort:'max'},
   setup:async scope=>{if(parent)ctx.agentPresets.composeFrom(scope,parent.ctx);else await ctx.agentPresets.mount(scope,'standard')}})
  handles.push(h);return h.agent
 }
 async function prompt(agent,text){agent.followup(createUserMessage({source:{kind:'user'},content:[{type:'text',text}]}));await agent.whenIdle()}
 const own=agent=>seen.filter(r=>r.sid===agent.session.id&&!r.planning)
 ctx.effect(()=>{
  let cancelled=false
  const timer=setTimeout(()=>{void run().catch(error=>writeFileSync(process.env.DPSWARM_BUDGET_PROBE_OUTPUT,JSON.stringify({passed:false,error:error.stack,seen,results},null,2)))},1400)
  async function run(){
   const prefix='worker-budget-loop-'+randomUUID(),previous=ctx.settings.get('dpswarm')
   try{
    await ctx.settings.update('dpswarm',{autoStart:false,sidecarUrl:process.env.DPSWARM_LOOP_SIDECAR_URL,workspace:process.env.DPSWARM_LOOP_WORKSPACE,workerBudgetMode:'manual',workerTokenLimit:200000,workerCallLimit:1,enabledSessions:[],cmEnabledSessions:[]})
    const root=await create(prefix+'-root')
    await prompt(root,'Record the root route.')
    const a=await create(prefix+'-a',root),b=await create(prefix+'-b',root)
    await Promise.all([prompt(a,'Assigned subtask A: describe one item.'),prompt(b,'Assigned subtask B: describe one other item.')])
    assert.equal(own(a).length,1);assert.equal(own(b).length,1)
    assert.equal(own(a)[0].maxTokens,1024,'manual limit must not raise native output default')
    const before=own(a).length;await prompt(a,'A second turn still shares this worker lifetime allowance.')
    assert.equal(own(a).length,before,'exhausted child must not dispatch another request')
    await prompt(root,'Lead continues after child exhausts.')
    assert.equal(own(root).length,2)
    const as=await ctx.dpswarmBudget.status(a),bs=await ctx.dpswarmBudget.status(b)
    assert.equal(as.calls,1);assert.equal(bs.calls,1);assert.equal((await ctx.dpswarmBudget.status(root)).lead_limited,false)
    results.push({case:'manual-independent-children-and-unlimited-lead',a:as,b:bs,leadRequests:own(root).length})
    await ctx.settings.update('dpswarm',{workerBudgetMode:'unlimited',workerTokenLimit:1,workerCallLimit:1})
    const free=await create(prefix+'-free',root)
    for(let i=0;i<3;i++)await prompt(free,'Unrestricted fixture turn '+i)
    assert.equal(own(free).length,3);assert.ok(own(free).every(r=>r.maxTokens===1024))
    assert.equal((await ctx.dpswarmBudget.status(free)).mode,'unlimited')
    assert.equal(seen.filter(r=>r.planning).length,0)
    results.push({case:'unlimited-ignores-retained-manual-values',calls:own(free).length})
    await ctx.settings.update('dpswarm',{workerBudgetMode:'auto'})
    const countBeforePlan=seen.length
    const allocation=await ctx.dpswarmBudget.plan(root,{task:'Assigned unique subtask: summarize the number 42, without solving another task.',tokenLimit:90000,callLimit:2,reason:'Current Lead chose two requests after reading the small assigned task.'})
    assert.equal(seen.length,countBeforePlan,'recording the Lead decision makes no model call')
    const auto=await create(prefix+'-auto',root)
    await prompt(auto,allocation.prompt)
    await prompt(auto,'Use the final allowed child request.')
    await prompt(auto,'This third request must not run.')
    assert.equal(own(auto).length,2);assert.equal(seen.filter(r=>r.planning).length,0)
    const autoStatus=await ctx.dpswarmBudget.status(auto)
    assert.equal(autoStatus.tokenLimit,90000);assert.equal(autoStatus.callLimit,2)
    assert.equal(autoStatus.observed_tokens_lower_bound,260)
    assert.equal(autoStatus.decision.reason,allocation.reason)
    results.push({case:'auto-actual-lead-decision-and-lifetime-stop',status:autoStatus})
    const missing=await create(prefix+'-missing',root)
    await prompt(missing,'No allocation supplied.')
    assert.equal(own(missing).length,0)
    const replay=await create(prefix+'-replay',root)
    await prompt(replay,allocation.prompt)
    assert.equal(own(replay).length,0)
    results.push({case:'missing-and-reused-allocation-block-before-model',requests:0})
    const toolLead=await create(prefix+'-tool-lead')
    await prompt(toolLead,'Read the native task and allocate its worker budget with dpswarm_prepare_worker.')
    const audit=await new AuditJournal({config:()=>ctx.settings.get('dpswarm')}).read(toolLead.session.id)
    const issued=audit.events.filter(e=>e.type==='dpswarm/worker-budget-allocation')
    assert.ok(toolLead.session.events.every(e=>!e.type.startsWith('dpswarm/')))
    assert.equal(issued.length,1,'actual Lead tool executes and durably records one decision')
    assert.ok(!toolLead.session.events.some(e=>e.type==='tool/result' && e.data?.isError),'tool output must validate')
    const native=await ctx.subagents.start('spawn',{signal:new AbortController().signal,parent:toolLead,label:'budget probe',prompt:[{type:'text',text:issued[0].data.prompt}],agentOptions:{model:'worker-fixture',provider:'worker-budget-fixture',reasoningEffort:'max'}})
    try {
     const outcome=await native.result
     assert.equal(outcome.stopReason,'completed',JSON.stringify(outcome))
     const child=ctx.sessions.get(native.id)
     assert.ok(child)
     const state=await ctx.dpswarmBudget.status({session:child})
     assert.equal(state.mode,'auto');assert.equal(state.callLimit,3);assert.equal(state.calls,1)
     assert.equal(state.decision.reason,issued[0].data.reason)
     results.push({case:'real-lead-tool-to-native-spawn',leadRequests:own(toolLead).length,status:state})
    }finally{await native.dispose()}
    const restored=await ctx.dpswarmBudget.status(root)
    assert.ok(restored.children.length>=4)
    if(cancelled)throw new Error('Probe cancelled')
    writeFileSync(process.env.DPSWARM_BUDGET_PROBE_OUTPUT,JSON.stringify({passed:true,external_model_calls:0,execution:'native DSH AgentLoop + standard preset + local deterministic adapter',results,seen},null,2))
   }finally{
    for(const h of handles.reverse())await h.dispose()
    await ctx.settings.replace('dpswarm',previous)
   }
  }
  return()=>{cancelled=true;clearTimeout(timer)}
 })
}
