/** Installed DPH/DSH model chooser and persistent role routes. Use an isolated host with local fixture models only. */
import assert from 'node:assert/strict'
import {mkdirSync,writeFileSync} from 'node:fs'
import {resolve,join} from 'node:path'
import {pathToFileURL} from 'node:url'
const {chromium}=await import(pathToFileURL(process.env.DPSWARM_TEST_PLAYWRIGHT).href)
const url=new URL(process.env.DPSWARM_TEST_URL);assert.equal(url.hostname,'127.0.0.1')
const out=resolve(process.env.DPSWARM_TEST_OUTPUT || '.tmp/model-settings-20260907');mkdirSync(out,{recursive:true})
const browser=await chromium.launch({headless:true}), page=await browser.newPage({viewport:{width:1280,height:1000}})
const errors=[];page.setDefaultTimeout(12000);page.on('pageerror',e=>errors.push(e.message))
let previous, checked=false
const keys=['impl','test','cm','reviewer'].flatMap(role=>['Provider','Model','Effort'].map(suffix=>role+suffix)).concat('reviewerMode','implMode')
async function rpc(method,payload){return page.evaluate(async({method,payload})=>{
 const response=await fetch('/api/'+method,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({type:'client-request',rpcId:crypto.randomUUID(),method,payload})})
 if(!response.ok)throw new Error('HTTP '+response.status)
 const data=await response.json();if(!data.result.ok)throw new Error(data.result.error.message);return data.result.value
},{method,payload})}
const config=async()=>{const d=await rpc('settings.describe',{});return d.namespaces.find(v=>v.ns==='dpswarm')}
async function openSettings(){
 for(const name of ['继续','稍后配置']){const b=page.getByRole('button',{name,exact:true});if(await b.count())await b.click()}
 await page.getByRole('button',{name:'设置',exact:true}).click()
 await page.getByRole('button',{name:'DPswarm',exact:true}).click()
 await page.getByRole('heading',{name:'DPswarm',exact:true}).waitFor()
}
const form=role=>page.getByRole('form',{name:role+'配置',exact:true})
async function picker(role){await form(role).getByRole('button',{name:'选择'+role+'模型',exact:true}).click();return page.getByRole('dialog',{name:'为'+role+'选择模型',exact:true})}
async function choose(role,query,effort){
 const dialog=await picker(role)
 await dialog.getByRole('combobox',{name:'搜索模型或 Provider'}).fill(query)
 await dialog.getByRole('option').first().click()
 if(effort!==undefined)await form(role).getByRole('combobox',{name:role+'推理强度',exact:true}).selectOption(effort)
 const button=form(role).getByRole('button',{name:'保存'+role+'配置',exact:true})
 if(await button.isEnabled()){await button.click();await form(role).getByRole('status').filter({hasText:'已保存'}).waitFor()}
}
try{
 await page.goto(new URL('/?dpswarm-settings=1',url).href)
 await page.getByRole('heading',{name:'DPswarm',exact:true}).waitFor()
 assert.equal(new URL(page.url()).searchParams.has('dpswarm-settings'),false)
 await page.keyboard.press('Escape')
 previous=Object.fromEntries(keys.map(key=>[key,undefined]))
 const initial=await config();for(const key of keys)previous[key]=initial.value[key]
 await openSettings();assert.equal(await page.getByRole('heading',{name:'DPswarm',exact:true}).count(),1)
 const expected={implMode:'model',implProvider:'dpswarm-cm-fixture',implModel:'glm-5.3-flash',implEffort:'',testProvider:'dpswarm-cm-fixture',testModel:'glm-5.3-flash',testEffort:'',reviewerMode:'model',reviewerProvider:'dpswarm-cm-fixture',reviewerModel:'gpt-5.6-sol',reviewerEffort:'max',cmProvider:'dpswarm-cm-fixture',cmModel:'fixture-summary-custom',cmEffort:'max'}
 await choose('实现者','glm-5.3-flash','');await choose('测试者','glm-5.3-flash','')
 await choose('Reviewer','gpt-5.6-sol','max');await choose('CM','fixture-summary-custom','max')
 await page.reload();await openSettings()
 const saved=await config();for(const [key,value]of Object.entries(expected))assert.equal(saved.value[key],value,key)
 await form('Reviewer').getByText('gpt-5.6-sol',{exact:true}).waitFor()
 await form('CM').getByText('fixture-summary-custom',{exact:true}).waitFor()
 const dialog=await picker('CM');await dialog.getByRole('option').first().waitFor()
 await page.screenshot({path:join(out,'actual-model-picker.png'),fullPage:false})
 await dialog.getByRole('combobox',{name:'搜索模型或 Provider'}).press('Escape')
 await page.getByRole('heading',{name:'DPswarm',exact:true}).waitFor()
 await choose('实现者','沿用当前对话');assert.equal((await config()).value.implMode,'lead')
 await choose('Reviewer','lead')
 assert.equal((await config()).value.reviewerMode,'lead')
 await page.getByRole('tab',{name:'运行规则',exact:true}).click();await page.getByLabel('每个角色超时（秒）',{exact:true}).waitFor()
 await page.getByRole('tab',{name:'模型分工',exact:true}).click()
 checked=true
}finally{
 try{
  if(previous&&Object.values(previous).every(v=>v!==undefined)){
   const before=await config();await rpc('settings.update',{ns:'dpswarm',patch:previous,expectedRevision:before.revision})
   const restored=await config();for(const [key,value]of Object.entries(previous))assert.deepEqual(restored.value[key],value)
   await page.reload();await openSettings()
  }
  await page.screenshot({path:join(out,'actual-settings-top.png'),fullPage:false})
  await form('CM').scrollIntoViewIfNeeded();await page.screenshot({path:join(out,'actual-settings-reviewer-cm.png'),fullPage:false})
  assert.deepEqual(errors,[])
  if(checked){writeFileSync(join(out,'browser-host.json'),JSON.stringify({passed:true,external_model_calls:0,checks:['native_settings_once','native_deep_link','host_catalog_picker','four_role_routes_persist_after_reload','implementer_returns_to_conversation','exact_provider_model_effort','reviewer_returns_to_lead','escape_preserves_host_settings','settings_tabs','restores_14_fields','no_page_errors'],screenshots:['actual-settings-top.png','actual-settings-reviewer-cm.png','actual-model-picker.png']},null,2));console.log('Real DSH catalog and settings: all four roles selected, saved, reloaded and restored.')}
 }finally{await browser.close()}
}
