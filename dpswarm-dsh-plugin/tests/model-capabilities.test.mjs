import test from 'node:test'
import assert from 'node:assert/strict'
import { HostModelRegistry } from '../lib/host-model-registry.js'
const route={role:'tester',provider:'p',model:'custom'}

test('exact host modalities distinguish unsupported, supported and unknown without model-name rules',async()=>{
  for(const [modalities,expected] of [[['text'],'unsupported'],[['text','image'],'supported'],[undefined,'unknown']]) {
    const registry=new HostModelRegistry(()=>({resolveCallConfig:async r=>r,
      resolveModelInfo:async(provider,id)=>({provider,id,inputModalities:modalities,secret:'private-token'})}))
    const result=await registry.resolve([route])
    assert.equal(result.capabilities[0].image_input,expected)
    assert.deepEqual(result.models,[route])
    assert.doesNotMatch(JSON.stringify(result),/private-token/)
    assert.match(registry.capabilityGuide(route),new RegExp(expected))
    assert.equal(registry.capabilityGuide({...route,model:'other'}),'')
  }
})
test('stale or missing modality metadata is not a routing substitution or proof of vision',async()=>{
  const registry=new HostModelRegistry(()=>({resolveCallConfig:async r=>r,
    resolveModelInfo:async()=>({provider:'other',id:'wrong',inputModalities:['image']})}))
  assert.equal((await registry.resolve([route])).capabilities[0].image_input,'unknown')
})

test('optional modality lookup timeout stays unknown but user cancellation still stops preflight',async()=>{
  const llm={resolveCallConfig:async r=>r,resolveModelInfo:()=>new Promise(()=>{})}
  const registry=new HostModelRegistry(()=>llm,{timeoutMs:20})
  const result=await registry.resolve([route])
  assert.deepEqual(result.models,[route]);assert.equal(result.capabilities[0].image_input,'unknown')
  const abort=new AbortController(),waiting=new HostModelRegistry(()=>llm,{timeoutMs:1000}).resolve([route],{signal:abort.signal})
  setTimeout(()=>abort.abort(),10)
  await assert.rejects(waiting,{code:'SUBAGENT_ABORTED'})
})

test('cold prompt advice waits for a resolved header, then uses only its exact route',async()=>{
  const registry=new HostModelRegistry(()=>({resolveCallConfig:async r=>r,
    resolveModelInfo:async(provider,id)=>({provider,id,inputModalities:['text']})}))
  await registry.resolve([route])
  const agent={options:route,session:{requestHeader:()=>undefined}}
  assert.equal(registry.capabilityGuideForAgent(agent),'')
  assert.equal(registry.capabilityGuideForAgent({session:{}}),'')
  agent.session.requestHeader=()=>({config:route})
  assert.match(registry.capabilityGuideForAgent(agent),/unsupported/)
  agent.session.requestHeader=()=>({config:{...route,model:'unresolved-other'}})
  assert.equal(registry.capabilityGuideForAgent(agent),'')
})

test('host model limits surface per-route request caps without trusting non-positive values',async()=>{
  const registry=new HostModelRegistry(()=>({resolveCallConfig:async r=>r,
    resolveModelInfo:async(provider,id)=>({provider,id,inputModalities:['text'],defaultMaxTokens:131072,context:{contextWindow:131072}})}))
  const result=await registry.resolve([route])
  assert.equal(result.capabilities[0].default_max_tokens,131072)
  assert.equal(result.capabilities[0].context_window,131072)
  assert.doesNotMatch(JSON.stringify(result),/131072.*secret|secret.*131072/)
})

test('missing, zero or non-integer model limits stay unknown, independent of modalities',async()=>{
  for(const limits of [{},{defaultMaxTokens:0},{defaultMaxTokens:'131072'},{defaultMaxTokens:131072.5},{context:{contextWindow:-1}},{context:null}]){
    const registry=new HostModelRegistry(()=>({resolveCallConfig:async r=>r,
      resolveModelInfo:async(provider,id)=>({provider,id,inputModalities:['text'],...limits})}))
    const row=(await registry.resolve([route])).capabilities[0]
    assert.equal(row.default_max_tokens,null,JSON.stringify(limits))
    assert.equal(row.context_window,null,JSON.stringify(limits))
    assert.equal(row.image_input,'unsupported','modalities stay authoritative on their own')
  }
})

test('resolveRouteCapability serves advisory limits outside preflight and caches them',async()=>{
  let lookups=0
  const registry=new HostModelRegistry(()=>({resolveModelInfo:async(provider,id)=>{lookups++
    return {provider:'other',id:'wrong',defaultMaxTokens:131072,context:{contextWindow:131072}}}}))
  assert.equal(await registry.resolveRouteCapability('p','custom'),null,'provider/id drift returns null without caching')
  const registry2=new HostModelRegistry(()=>({resolveModelInfo:async(provider,id)=>{lookups++
    return {provider,id,defaultMaxTokens:131072,context:{contextWindow:131072}}}}))
  const capability=await registry2.resolveRouteCapability('p','custom')
  assert.equal(capability.default_max_tokens,131072)
  assert.equal(capability.context_window,131072)
  const again=await registry2.resolveRouteCapability('p','custom')
  assert.equal(again,capability,'second lookup is served from cache')
  assert.equal(lookups,2)
  const broken=new HostModelRegistry(()=>({resolveModelInfo:async()=>{throw new Error('metadata down')}}))
  assert.equal(await broken.resolveRouteCapability('p','custom'),null,'an unavailable lookup is advisory null, never a rejection')
  assert.equal(await new HostModelRegistry(()=>null).resolveRouteCapability('p','custom'),null)
})
