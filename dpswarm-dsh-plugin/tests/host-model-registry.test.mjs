import assert from 'node:assert/strict'
import test from 'node:test'
import { HostModelRegistry } from '../lib/host-model-registry.js'

const route = { role: 'implementer', provider: 'deepseek-official', model: 'deepseek-v4.1-flash-expires-on-0910', reasoningEffort: 'max' }

test('exact host resolution accepts an unlisted custom model; projection contains only route data', async () => {
  let resolved = 0
  const registry = new HostModelRegistry(() => ({
    async resolveCallConfig(input) { resolved++; return { ...input, maxTokens: 256000, arbitraryPrivateMetadata: 'do-not-copy' } },
    listModels() { throw new Error('Advisory listing must not be an availability gate') }
  }))
  const receipt = await registry.resolve([route, { ...route, role: 'lead' }])
  assert.equal(resolved, 2)
  assert.deepEqual(receipt.models[0], route)
  assert.doesNotMatch(JSON.stringify(receipt), /do-not-copy|maxTokens|arbitraryPrivateMetadata/)
  let request
  await registry.publish({ async call(...args) { request=args; return {ok:true} } }, receipt)
  assert.deepEqual(request, ['POST','/api/models/host-catalog',{source:'dph',models:[{provider:route.provider,model:route.model}]}])
})

test('provider removal cannot reuse a previously resolved model', async () => {
  let available = true
  const registry = new HostModelRegistry(() => ({ async resolveCallConfig(input) {
    if (!available) throw Object.assign(new Error('private provider configuration'),{code:'UNKNOWN_PROVIDER'})
    return input
  } }))
  const previous = await registry.resolve([route]); available=false
  await assert.rejects(registry.resolve([route],{expected:previous}), error => error.code==='HOST_MODEL_UNAVAILABLE' && !error.message.includes('private provider configuration'))
})

test('explicit effort and model substitution are rejected', async () => {
  for (const mutate of [input=>({...input,reasoningEffort:'high'}),input=>({...input,model:'deepseek-v4-flash'})]) {
    const registry = new HostModelRegistry(()=>({async resolveCallConfig(input){return mutate(input)}}))
    await assert.rejects(registry.resolve([route]), {code:'HOST_MODEL_ROUTE_DRIFT'})
  }
})

test('implicit reasoning defaults are frozen and revalidated', async () => {
  let effort='high'
  const registry=new HostModelRegistry(()=>({async resolveCallConfig(input){return {...input,reasoningEffort:effort}}}))
  const selection={...route};delete selection.reasoningEffort
  const first=await registry.resolve([selection]);assert.equal(first.models[0].reasoningEffort,'high')
  effort='max'
  await assert.rejects(registry.resolve([selection],{expected:first}), {code:'HOST_MODEL_ROUTE_DRIFT'})
})

test('missing service, malformed routes, timeouts and cancellation fail before publication', async () => {
  await assert.rejects(new HostModelRegistry(()=>undefined).resolve([route]),{code:'HOST_MODEL_REGISTRY_UNAVAILABLE'})
  const available=new HostModelRegistry(()=>({async resolveCallConfig(input){return input}}))
  await assert.rejects(available.resolve([route,route]),{code:'HOST_MODEL_ROUTE_INVALID'})
  const stalled=new HostModelRegistry(()=>({resolveCallConfig(){return new Promise(()=>{})}}),{timeoutMs:15})
  await assert.rejects(stalled.resolve([route]),{code:'HOST_MODEL_REGISTRY_TIMEOUT'})
  const abort=new AbortController();abort.abort()
  await assert.rejects(stalled.resolve([route],{signal:abort.signal}),{code:'SUBAGENT_ABORTED'})
})
