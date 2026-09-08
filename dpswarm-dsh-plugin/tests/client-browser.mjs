/** Render the shipped client with real React/Chromium and a controlled settings transport.
 * This checks UI behavior, not a real DSH model run. Dependencies are supplied by the test host.
 */
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { readFileSync, mkdirSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { resolveHostRoot } from '../lib/host-modules.js'

const repo=resolve(dirname(fileURLToPath(import.meta.url)),'../..')
const out=process.env.DPSWARM_TEST_OUTPUT?resolve(process.env.DPSWARM_TEST_OUTPUT):join(repo,'.tmp/model-settings-20260907/browser');mkdirSync(out,{recursive:true})
const esbuild=await import(pathToFileURL(process.env.DPSWARM_TEST_ESBUILD).href)
const {chromium}=await import(pathToFileURL(process.env.DPSWARM_TEST_PLAYWRIGHT).href)
const host=resolveHostRoot(), anchor=join(host,'dsh-client-ui-trajectory/lib/index.js')
const require=createRequire(anchor), react=require.resolve('react'), dom=require.resolve('react-dom/client')
const entry=`import React from ${JSON.stringify(react)};import {createRoot} from ${JSON.stringify(dom)};
window.__ModuleLoader__={load(mod){window.dpsModule=mod.factory(id=>{if(id==='react')return React;throw new Error('optional icon absent')})}};
window.mount=()=>{
 let snapshot={status:'ready',writable:true,mode:'host',revision:1,value:{enabledSessions:[],sidecarUrl:'http://127.0.0.1:8791',implMode:'',implProvider:'',testProvider:'',implModel:'',testModel:'glm-5.3-flash',workerTimeoutSeconds:600,cmEnabledSessions:[],cmProvider:'deepseek',cmModel:'deepseek-v4-flash',cmEffort:'off',reviewerMode:'lead',reviewerProvider:'',reviewerModel:'',reviewerEffort:''}};
 const listeners=new Set();window.calls=[];window.fetches=[];
 const scope={getSnapshot:()=>snapshot,subscribe:f=>{listeners.add(f);return()=>listeners.delete(f)}};
 const view=()=>({ns:'dpswarm',revision:snapshot.revision,value:snapshot.value});
 const mirror={acceptView(next){snapshot={...snapshot,value:next.value,revision:next.revision};for(const f of listeners)f()}};
 const api={async mutate(request){window.calls.push(request);if(window.failNext){window.failNext=false;return {result:{ok:false,error:{message:'fixture host write rejected'}}}}if(request.expectedRevision!==snapshot.revision)return{result:{ok:false,error:{message:'stale revision'}}};
  const next={...snapshot.value};for(const op of request.ops)next[op.path[0]]=op.value;
  return{result:{ok:true,value:{ns:'dpswarm',revision:snapshot.revision+1,value:next}}};},async describe(){return {result:{ok:true,value:{namespaces:[view()]}}}}};
 window.scope=scope;window.setUnavailable=()=>{snapshot={...snapshot,status:'unavailable',writable:false,mode:'memory'};for(const f of listeners)f()};
 window.fetch=async(url,options)=>{window.fetches.push({url,headers:options?.headers});return{ok:true,json:async()=>({bridge:{session_isolation:true},state:'not_started',snapshot:null})}};
 const effort=(...ids)=>({efforts:ids.map(id=>({id,name:id}))});
 window.catalogGroups=[{id:'deepseek',name:'DeepSeek',models:[{id:'deepseek-v4-flash',name:'DeepSeek V4 Flash',reasoning:effort('off','high')},{id:'deepseek-v4-pro',name:'DeepSeek V4 Pro',reasoning:effort('off','max')}]},
 {id:'coding-plan',name:'智谱 Coding Plan',models:[{id:'glm-5.3-flash',name:'GLM-5.3-Flash',reasoning:effort('high')}]},
 {id:'gpt',name:'GPT Provider',models:[{id:'gpt-5.6-terra',name:'GPT Terra',reasoning:effort('max')},{id:'gpt-5.6-sol',name:'GPT Sol',reasoning:effort('high','max')}]}];
 window.catalogReads=0;const llm={async models(){window.catalogReads++;if(window.catalogError)return{result:{ok:false,error:{message:'fixture catalog unavailable'}}};return{result:{ok:true,value:{groups:window.catalogGroups,failures:window.catalogPartial?[{id:'broken',name:'Broken Provider',message:'offline'}]:[]}}}}};
 const slots={};window.registrations=[];window.dpsModule.apply({settingsScope:{bind:()=>scope,describe:()=>mirror},connection:{isLoopback:true,api:{settings:api,llm}},slots:{inject:(name,f)=>f(),register:(spec,component)=>{slots[spec.name]=component;window.registrations.push({name:spec.name,id:spec.id,label:spec.label?.()})}},logger:{warn:console.warn}});
 createRoot(document.getElementById('card')).render(React.createElement(slots['settings.plugin.item']));
 createRoot(document.getElementById('settings')).render(React.createElement(slots['settings.section'],{close:()=>{window.leadReturn=true}}));
 for(const id of ['session-a','session-b'])createRoot(document.getElementById(id)).render(React.createElement(slots['conversation.input.left'],{sessionId:id}));
};`
await esbuild.build({stdin:{contents:entry,resolveDir:repo,sourcefile:'fixture-entry.js'},bundle:true,format:'iife',platform:'browser',outfile:join(out,'fixture.js'),define:{'process.env.NODE_ENV':'"development"'},logLevel:'silent'})
const css=`:root{color-scheme:light;--dsw-alias-label-primary:#24272e;--dsw-alias-label-secondary:#5b6472;--dsw-alias-label-tertiary:#9298a5;--dsw-alias-bg-layer-2:#fff;--dsw-alias-bg-layer-3:#f8fafc;--dsw-alias-border-l2:#dce1e8;--dsw-alias-border-inverted:#d0d6df;--dsw-specific-menu:#fff;--dsw-alias-state-warn-primary:#b88210;--dsw-alias-state-success-primary:#178350;--dsw-alias-state-error-primary:#c33647;--dsw-alias-brand-primary:#3b64cb;--dsw-shadow-lv3:0 8px 24px #19223626}body{font-family:'Segoe UI','Microsoft YaHei',sans-serif;padding:20px;margin:0;background:#f5f6f8;color:var(--dsw-alias-label-primary)}main{max-width:780px;margin:auto}ul{padding:0;list-style:none}aside{display:flex;gap:40px;padding:30px 0;position:relative}h1{font-size:22px}body.dark{background:#181c22;color:#e4e8ef;--dsw-alias-label-primary:#e4e8ef;--dsw-alias-label-secondary:#adb7c8;--dsw-alias-bg-layer-2:#222831;--dsw-alias-bg-layer-3:#1c222a;--dsw-specific-menu:#222831;--dsw-alias-border-l2:#424b59;--dsw-alias-border-inverted:#424b59}`
const html=`<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>${css}</style><main><h1>设置 → DPswarm</h1><div id="settings"></div><ul id="card"></ul><aside><section>A 任务<div id="session-a"></div></section><section>B 任务<div id="session-b"></div></section></aside></main><script src="/fixture.js"></script><script src="/client.js"></script><script>mount()</script></html>`
const server=createServer((req,res)=>{res.setHeader('Content-Type',req.url==='/'?'text/html; charset=utf-8':'text/javascript');res.end(req.url==='/fixture.js'?readFileSync(join(out,'fixture.js')):req.url==='/client.js'?readFileSync(join(repo,'dpswarm-dsh-plugin/lib/client.js')):html)})
await new Promise(r=>server.listen(0,'127.0.0.1',r));let browser
try{
 browser=await chromium.launch({headless:true});const page=await browser.newPage({viewport:{width:1040,height:920}}), errors=[], checks=[]
 page.setDefaultTimeout(10000);page.on('pageerror',e=>errors.push(e.message))
 await page.goto(`http://127.0.0.1:${server.address().port}`)
 const form=role=>page.getByRole('form',{name:role+'配置',exact:true})
 const openPicker=async role=>{await form(role).getByRole('button',{name:'选择'+role+'模型',exact:true}).click();await page.getByRole('dialog',{name:'为'+role+'选择模型',exact:true}).waitFor()}
 const search=()=>page.getByRole('combobox',{name:'搜索模型或 Provider'})
 const save=role=>form(role).getByRole('button',{name:'保存'+role+'配置',exact:true}).click()
 const pick=async(role,query)=>{await openPicker(role);await search().fill(query);await page.getByRole('dialog').getByRole('option').first().click()}
 const launch=id=>page.locator('#'+id+' button'), team=()=>page.getByRole('switch',{name:'当前任务使用固定团队'}), cm=()=>page.getByRole('switch',{name:'当前任务使用 CM',exact:true})
 await form('CM').waitFor()
 assert.equal(await page.evaluate(()=>registrations.filter(r=>r.name==='settings.section'&&r.id==='dpswarm'&&r.label==='DPswarm').length),1);checks.push('native_section')
 assert.equal(await form('Reviewer').getByText('当前 Lead',{exact:true}).count(),1);checks.push('reviewer_default_lead');assert.equal(await form('实现者').getByText('沿用当前对话模型',{exact:true}).count(),1);assert.equal(await form('实现者').getByRole('combobox',{name:'实现者推理强度',exact:true}).count(),0);checks.push('implementer_defaults_to_conversation')
 assert.deepEqual(await page.evaluate(()=>calls),[]);checks.push('opening_settings_does_not_write')
 assert.equal(await form('测试者').getByText('待配置',{exact:true}).count(),1)
 assert.equal(await form('实现者').getByText('待配置',{exact:true}).count(),0)
 await launch('session-a').click();assert.equal(await team().isDisabled(),true);assert.equal(await cm().isDisabled(),false)
 const missingHint=page.getByText('尚未配置：测试者的 Provider。请在设置 → DPswarm 选择模型并保存。',{exact:true})
 await missingHint.waitFor();await page.screenshot({path:join(out,'missing-tester.png'),fullPage:true})
 await page.keyboard.press('Escape');checks.push('missing_tester_provider_explained','inherited_roles_need_no_route','cm_available_without_team_route')
 await page.getByRole('button',{name:'返回任务调整主模型'}).click();assert.equal(await page.evaluate(()=>leadReturn),true)
 await openPicker('CM');await search().fill('coding-plan glm');await page.getByRole('dialog').getByRole('option',{name:/GLM-5.3-Flash/}).waitFor()
 await search().press('ArrowDown');await search().press('Enter')
 assert.equal(await page.getByRole('dialog').count(),0)
 assert.equal(await form('CM').getByRole('combobox',{name:'CM推理强度',exact:true}).inputValue(),'')
 assert.equal(await form('CM').getByRole('combobox',{name:'CM推理强度',exact:true}).locator('option[value="off"]').count(),0)
 assert.equal(await page.evaluate(()=>scope.getSnapshot().value.cmModel),'deepseek-v4-flash');checks.push('search_keyboard_selection','unsupported_effort_cleared_in_draft','unsaved_draft_not_applied')
 await page.evaluate(()=>{failNext=true});await save('CM');await form('CM').getByRole('alert').filter({hasText:'fixture host write rejected'}).waitFor()
 assert.equal(await page.evaluate(()=>scope.getSnapshot().value.cmModel),'deepseek-v4-flash')
 assert.equal(await form('CM').getByText('GLM-5.3-Flash',{exact:true}).count(),1)
 await save('CM');await form('CM').getByRole('status').filter({hasText:'已保存'}).waitFor()
 assert.equal(await page.evaluate(()=>scope.getSnapshot().value.cmProvider),'coding-plan');checks.push('rejected_save_preserves_draft','atomic_cm_route_save')
 await pick('测试者','coding-plan')
 await launch('session-a').click();assert.equal(await team().isDisabled(),true);await page.keyboard.press('Escape')
 await save('测试者');await page.waitForFunction(()=>scope.getSnapshot().value.testProvider==='coding-plan')
 assert.equal(await form('测试者').getByText('待配置',{exact:true}).count(),0)
 await launch('session-a').click();assert.equal(await team().isDisabled(),false);assert.equal(await cm().isDisabled(),false)
 assert.equal(await missingHint.count(),0);assert.equal(await page.evaluate(()=>scope.getSnapshot().value.implProvider),'')
 assert.deepEqual(await page.evaluate(()=>scope.getSnapshot().value.enabledSessions),[])
 await page.keyboard.press('Escape');checks.push('unsaved_tester_does_not_unlock_team','saving_only_tester_unlocks_inherited_team')
 await pick('实现者','coding-plan');await save('实现者')
 assert.equal(await page.evaluate(()=>scope.getSnapshot().value.implMode),'model')
 await openPicker('实现者');await search().fill('沿用当前对话');await page.getByRole('dialog').getByRole('option',{name:/沿用当前对话模型/}).click();await save('实现者')
 await page.waitForFunction(()=>scope.getSnapshot().value.implMode==='lead')
 const implWrite=await page.evaluate(()=>calls.filter(c=>c.ops.some(o=>o.path[0]==='implMode')).at(-1));assert.equal(implWrite.ops.length,4)
 assert.equal(await form('实现者').getByRole('combobox',{name:'实现者推理强度',exact:true}).count(),0);checks.push('implementer_override_and_atomic_return_to_conversation')
 await pick('Reviewer','gpt terra');await form('Reviewer').getByRole('combobox',{name:'Reviewer推理强度',exact:true}).selectOption('max')
 await save('Reviewer');await form('Reviewer').getByRole('status').filter({hasText:'已保存'}).waitFor()
 assert.equal(await page.evaluate(()=>scope.getSnapshot().value.reviewerMode),'model')
 assert.equal(await page.evaluate(()=>scope.getSnapshot().value.reviewerModel),'gpt-5.6-terra')
 assert.equal(await page.evaluate(()=>scope.getSnapshot().value.reviewerEffort),'max');checks.push('workers_select_host_models','independent_reviewer_saved')
 const reviewerWrite=await page.evaluate(()=>calls.filter(c=>c.ops.some(o=>o.path[0]==='reviewerMode')).at(-1))
 assert.equal(reviewerWrite.ops.length,4)
 await openPicker('Reviewer');await search().fill('lead');await page.getByRole('dialog').getByRole('option',{name:/沿用 Lead/}).click();await save('Reviewer')
 await page.waitForFunction(()=>scope.getSnapshot().value.reviewerMode==='lead');checks.push('reviewer_can_return_to_lead')
 assert.deepEqual(await page.evaluate(()=>scope.getSnapshot().value.enabledSessions),[])
 assert.deepEqual(await page.evaluate(()=>scope.getSnapshot().value.cmEnabledSessions),[]);checks.push('model_selection_never_enables_task')
 await openPicker('CM');await search().fill('no-such-model');await page.getByText('没有找到匹配模型',{exact:true}).waitFor()
 await search().press('Escape');assert.equal(await page.getByRole('dialog').count(),0);assert.equal(await form('CM').isVisible(),true);checks.push('empty_search','escape_returns_to_settings')
 await page.evaluate(()=>{catalogPartial=true});await openPicker('CM');await page.getByText(/部分 Provider 未能加载/).waitFor();assert.ok(await page.getByRole('dialog').getByRole('option').count()>0)
 await page.getByRole('button',{name:'关闭模型选择'}).click();checks.push('partial_provider_failure_keeps_healthy_models')
 await page.evaluate(()=>{catalogError=true});await openPicker('CM');await page.getByRole('alert').filter({hasText:'fixture catalog unavailable'}).waitFor()
 assert.ok(await page.getByRole('dialog').getByRole('option').count()>0);await page.getByRole('button',{name:'关闭模型选择'}).click();checks.push('catalog_failure_keeps_last_known_routes')
 await page.evaluate(()=>{catalogError=false;catalogPartial=false})
 await page.getByRole('tab',{name:'高级设置',exact:true}).click();assert.equal(await form('CM').isVisible(),false)
 await page.getByLabel('控制服务地址',{exact:true}).waitFor()
 await page.getByRole('tab',{name:'高级设置',exact:true}).press('Home');assert.equal(await form('CM').isVisible(),true);checks.push('tab_navigation_keyboard')
 await form('CM').locator('summary').click();await page.getByLabel('CM模型 ID',{exact:true}).fill('custom-hidden-model')
 await page.getByLabel('CM Provider',{exact:true}).fill('custom-route');await save('CM')
 await page.waitForFunction(()=>scope.getSnapshot().value.cmModel==='custom-hidden-model');checks.push('manual_custom_route_preserved')
 await form('CM').locator('summary').click()
 await pick('CM','deepseek-v4-flash');await form('CM').getByRole('combobox',{name:'CM推理强度',exact:true}).selectOption('off');await save('CM')
 await page.waitForFunction(()=>scope.getSnapshot().value.cmModel==='deepseek-v4-flash')
 await page.screenshot({path:join(out,'desktop.png'),fullPage:true})
 await openPicker('CM');await page.screenshot({path:join(out,'model-picker.png'),fullPage:true});await page.getByRole('button',{name:'关闭模型选择'}).click()
 await launch('session-a').click();assert.equal(await team().isChecked(),false);assert.equal(await cm().isChecked(),false)
 await cm().click();await page.waitForFunction(()=>scope.getSnapshot().value.cmEnabledSessions.includes('session-a'))
 assert.deepEqual(await page.evaluate(()=>fetches),[]);assert.deepEqual(await page.evaluate(()=>scope.getSnapshot().value.enabledSessions),[]);checks.push('cm_only_no_team_service')
 await page.keyboard.press('Escape');await launch('session-b').click();assert.equal(await cm().isChecked(),false);checks.push('session_isolation')
 await page.keyboard.press('Escape');await launch('session-a').click();await cm().click();await team().click()
 await page.waitForFunction(()=>scope.getSnapshot().value.enabledSessions.includes('session-a'));await team().click()
 await page.waitForFunction(()=>scope.getSnapshot().value.enabledSessions.length===0);await page.keyboard.press('Escape');checks.push('explicit_team_enable_disable')
 await page.setViewportSize({width:390,height:1000});await page.evaluate(()=>document.body.classList.add('dark'))
 await page.screenshot({path:join(out,'mobile-dark.png'),fullPage:true})
 await openPicker('CM');await page.screenshot({path:join(out,'mobile-picker-dark.png'),fullPage:true})
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false)
 const bounds=await page.getByRole('dialog').boundingBox();assert.ok(bounds.x>=0&&bounds.width<=390);checks.push('mobile_dark_no_overflow')
 await page.getByRole('button',{name:'关闭模型选择'}).click();await page.evaluate(()=>setUnavailable())
 await page.waitForFunction(()=>document.querySelector('[aria-label="选择CM模型"]').disabled);checks.push('readonly_settings_block')
 assert.deepEqual(errors,[]);checks.push('no_page_errors')
 writeFileSync(join(out,'results.json'),JSON.stringify({checks,model_calls:0,transport:'controlled host model catalog, settings and sidecar fixture',screenshots:['missing-tester.png','desktop.png','model-picker.png','mobile-dark.png','mobile-picker-dark.png']},null,2))
 console.log('Browser checks passed: '+checks.length+'; host-model selection, Reviewer inheritance, persistence boundaries and mobile layout.')
}finally{await browser?.close();await new Promise(r=>server.close(r))}
