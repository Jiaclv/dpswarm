/** Offline browser regression for per-subagent limits. No DPH host or model calls.
 * Uses an existing React-only fixture bundle; override DPSWARM_TEST_REACT_FIXTURE if needed.
 * The shipped client.js is loaded separately, so this always exercises current product code.
 */
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { readFileSync, mkdirSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const out = resolve(process.env.DPSWARM_TEST_OUTPUT || join(repo, '.tmp/worker-budget-settings-20260908'))
mkdirSync(out, { recursive: true })
const playwrightModule = process.env.DPSWARM_TEST_PLAYWRIGHT
  ? pathToFileURL(resolve(process.env.DPSWARM_TEST_PLAYWRIGHT)).href
  : 'playwright'
const { chromium } = await import(playwrightModule)
const fixturePath = process.env.DPSWARM_TEST_REACT_FIXTURE || join(repo, '.tmp/model-settings-20260907/browser/fixture.js')
let fixture = readFileSync(fixturePath, 'utf8')
const marker = 'window.scope = scope;'
assert.equal(fixture.split(marker).length, 2, 'Known isolated fixture hook required; never patch a live host')
fixture = fixture.replace(marker, marker + `
window.fixturePatch = patch => mirror.acceptView({ns:'dpswarm',revision:snapshot.revision+1,value:{...snapshot.value,...patch}});
window.fixtureState = patch => {snapshot={...snapshot,...patch};for(const f of listeners)f()};
`)
const css = `:root{color-scheme:light;--dsw-alias-label-primary:#24272e;--dsw-alias-label-secondary:#5b6472;--dsw-alias-label-tertiary:#778292;--dsw-alias-bg-layer-2:#fff;--dsw-alias-bg-layer-3:#f8fafc;--dsw-alias-bg-module-platform:#f0f3f7;--dsw-alias-border-l2:#dce1e8;--dsw-alias-border-inverted:#d0d6df;--dsw-specific-menu:#fff;--dsw-alias-state-warn-primary:#b88210;--dsw-alias-state-success-primary:#178350;--dsw-alias-state-error-primary:#c33647;--dsw-alias-brand-primary:#3b64cb;--dsw-shadow-lv3:0 8px 24px #19223626}body{font-family:'Segoe UI','Microsoft YaHei',sans-serif;padding:20px;margin:0;background:#f5f6f8;color:var(--dsw-alias-label-primary)}main{max-width:780px;margin:auto}ul{padding:0;list-style:none}aside{display:flex;gap:40px;padding:30px 0;position:relative}h1{font-size:22px}body.dark{color-scheme:dark;background:#181c22;color:#e4e8ef;--dsw-alias-label-primary:#e4e8ef;--dsw-alias-label-secondary:#adb7c8;--dsw-alias-label-tertiary:#a1acbd;--dsw-alias-bg-layer-2:#222831;--dsw-alias-bg-layer-3:#1c222a;--dsw-alias-bg-module-platform:#282f39;--dsw-specific-menu:#222831;--dsw-alias-border-l2:#424b59;--dsw-alias-border-inverted:#424b59}`
const html = `<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>${css}</style><main><h1>设置 → DPswarm</h1><div id="settings"></div><ul id="card"></ul><aside><section>A 任务<div id="session-a"></div></section><section>B 任务<div id="session-b"></div></section></aside></main><script src="/fixture.js"></script><script src="/client.js"></script><script>mount()</script></html>`
const server = createServer((req, res) => {
 const paths = { '/': ['text/html; charset=utf-8', html], '/fixture.js': ['text/javascript', fixture], '/client.js': ['text/javascript', readFileSync(join(repo, 'dpswarm-dsh-plugin/lib/client.js'))] }
 const item = paths[req.url]; if (!item) { res.writeHead(404).end(); return }
 res.setHeader('Content-Type', item[0]); res.end(item[1])
})
await new Promise(r => server.listen(0, '127.0.0.1', r))
let browser
const checks = [], errors = [], blocked = []
try {
 browser = await chromium.launch({headless:true})
 const page = await browser.newPage({viewport:{width:1040,height:1050}})
 page.setDefaultTimeout(10000)
 page.on('pageerror', e => errors.push(e.message))
 const origin = `http://127.0.0.1:${server.address().port}`
 await page.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : (blocked.push(route.request().url()), route.abort()))
 await page.goto(origin)
 await page.getByRole('tab',{name:'预算与运行',exact:true}).click()
 const form = page.getByRole('form',{name:'子 agent 限额配置',exact:true})
 const mode = name => form.getByRole('radio',{name,exact:true})
 const token = () => form.getByRole('textbox',{name:'每个子 agent 的 token 限额',exact:true})
 const calls = () => form.getByRole('textbox',{name:'每个子 agent 的调用次数',exact:true})
 const save = () => form.getByRole('button',{name:'保存子 agent 限额',exact:true}).click()
 const persisted = () => page.evaluate(()=>scope.getSnapshot().value)
 assert.equal(await mode('不做限制').isChecked(),true)
 assert.equal(await token().count(),0)
 assert.deepEqual(await page.evaluate(()=>window.calls),[])
 checks.push('legacy_defaults_to_unlimited_without_write')
 await mode('手动填写').check()
 assert.equal(await token().inputValue(),'600000');assert.equal(await calls().inputValue(),'28')
 await token().fill('1200000');await calls().fill('56')
 assert.equal((await persisted()).workerTokenLimit,undefined)
 await mode('Lead 自动分配').check();assert.equal(await token().count(),0)
 await mode('手动填写').check();assert.equal(await token().inputValue(),'1200000')
 checks.push('manual_defaults_are_draft_only','mode_switch_retains_unsaved_manual_values')
 await page.evaluate(()=>window.failNext=true)
 await save();await form.getByRole('alert').filter({hasText:'fixture host write rejected'}).waitFor()
 assert.equal((await persisted()).workerBudgetMode,undefined);assert.equal(await token().inputValue(),'1200000')
 await save();await form.getByRole('status').filter({hasText:'已保存'}).waitFor()
 assert.equal((await persisted()).workerBudgetMode,'manual');assert.equal((await persisted()).workerTokenLimit,1200000);assert.equal((await persisted()).workerCallLimit,56)
 const mutation=await page.evaluate(()=>window.calls.at(-1))
 assert.deepEqual(mutation.ops.map(x=>x.path[0]),['workerBudgetMode','workerTokenLimit','workerCallLimit'])
 checks.push('rejected_save_retains_draft','manual_limits_saved_atomically')
 await page.screenshot({path:join(out,'manual-desktop.png'),fullPage:true})
 await mode('Lead 自动分配').check();await save()
 await page.waitForFunction(()=>scope.getSnapshot().value.workerBudgetMode==='auto')
 assert.equal((await persisted()).workerTokenLimit,1200000);assert.equal((await persisted()).workerCallLimit,56)
 assert.equal(await token().count(),0)
 assert.equal(await form.getByText('当前 Lead 读完任务后，在正常派发流程中决定各子 agent 的 token 限额、调用次数和理由；不额外调用评估模型。',{exact:true}).count(),1)
 assert.deepEqual(await page.evaluate(()=>window.calls.at(-1).ops.map(x=>x.path[0])),['workerBudgetMode'])
 checks.push('auto_preserves_ignored_manual_limits','auto_has_no_fabricated_decision_or_extra_ceiling')
 await page.screenshot({path:join(out,'auto-desktop.png'),fullPage:true})
 await mode('不做限制').check();await save();await page.waitForFunction(()=>scope.getSnapshot().value.workerBudgetMode==='unlimited')
 assert.deepEqual(await page.evaluate(()=>window.calls.at(-1).ops.map(x=>x.path[0])),['workerBudgetMode'])
 assert.equal((await persisted()).workerTokenLimit,1200000)
 checks.push('unlimited_only_changes_mode')
 await mode('手动填写').check()
 for(const invalid of ['', '0', '-1', '1.5', '1e3', 'NaN', '9007199254740992']) {
  await token().fill(invalid);const before=await page.evaluate(()=>window.calls.length)
  await save();await form.getByRole('alert').filter({hasText:'请填写有效的子 agent token 限额和调用次数。'}).waitFor()
  assert.equal(await token().getAttribute('aria-invalid'),'true');assert.equal(await page.evaluate(()=>window.calls.length),before)
 }
 await token().fill('9007199254740991');await calls().fill('0')
 const before=await page.evaluate(()=>window.calls.length)
 await save();assert.equal(await calls().getAttribute('aria-invalid'),'true');assert.equal(await page.evaluate(()=>window.calls.length),before)
 await calls().fill('28');await save();await page.waitForFunction(()=>scope.getSnapshot().value.workerTokenLimit===9007199254740991)
 checks.push('manual_rejects_zero_negative_decimal_exponent_blank_and_unsafe_integer','both_limits_required','max_safe_integer_supported')
 await token().fill('invalid');await mode('Lead 自动分配').check();await save();await page.waitForFunction(()=>scope.getSnapshot().value.workerBudgetMode==='auto')
 assert.equal((await persisted()).workerTokenLimit,9007199254740991)
 checks.push('nonmanual_save_ignores_invalid_hidden_draft')
 await page.evaluate(()=>fixturePatch({workerTokenLimit:600000,workerCallLimit:28,workerBudgetMode:'auto',workerBudgetSessionOverrides:[{sessionId:'session-a',mode:'manual',tokenLimit:123456,callLimit:45}]}))
 await page.locator('#session-a button').click()
 const summary=()=>page.getByLabel('当前任务子 agent 限额配置',{exact:true})
 await summary().getByText('123,456 token · 45 次调用',{exact:true}).waitFor()
 await summary().getByText('此任务使用独立的子 agent 限额配置',{exact:true}).waitFor()
 await page.keyboard.press('Escape');await page.locator('#session-b button').click()
 await summary().getByText('每次派发前由 Lead 分别决定',{exact:true}).waitFor()
 assert.equal(await summary().getByText(/123,456/).count(),0)
 checks.push('root_session_override_priority','other_sessions_keep_global_mode')
 await page.keyboard.press('Escape');await page.getByRole('tab',{name:'模型分工',exact:true}).click()
 await page.locator('#session-b button').click();await summary().getByRole('button',{name:'调整限额 ↗',exact:true}).click()
 await page.waitForFunction(()=>document.getElementById('dps-tab-rules').getAttribute('aria-selected')==='true')
 await page.keyboard.press('Escape');checks.push('composer_shortcut_opens_budget_tab')
 await mode('手动填写').check();await token().fill('600000');await calls().fill('28');await save();await page.waitForFunction(()=>scope.getSnapshot().value.workerBudgetMode==='manual')
 await page.setViewportSize({width:390,height:1000});await page.evaluate(()=>document.body.classList.add('dark'))
 await page.screenshot({path:join(out,'manual-mobile-dark.png'),fullPage:true})
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false)
 const bounds=await form.boundingBox();assert.ok(bounds.width<=390 && bounds.x>=0)
 checks.push('mobile_dark_no_horizontal_overflow')
 await page.evaluate(()=>setUnavailable())
 await page.waitForFunction(()=>document.querySelector('[aria-label="子 agent 限额模式"]').disabled)
 assert.equal(await mode('不做限制').isDisabled(),true);assert.equal(await token().isDisabled(),true)
 assert.equal(await form.getByRole('button',{name:'保存子 agent 限额',exact:true}).isDisabled(),true)
 checks.push('readonly_prevents_edit_and_save')
 assert.deepEqual(await page.evaluate(()=>window.fetches),[])
 assert.deepEqual(blocked,[]);assert.deepEqual(errors,[])
 checks.push('no_model_or_sidecar_calls','no_external_requests','no_browser_errors')
 writeFileSync(join(out,'results.json'),JSON.stringify({checks,model_calls:0,transport:'isolated React and mock host; no DPH instance',screenshots:['manual-desktop.png','auto-desktop.png','manual-mobile-dark.png']},null,2))
 console.log(`Budget UI browser checks passed: ${checks.length}. Output: ${out}`)
} finally {await browser?.close();await new Promise(r=>server.close(r))}
