/**
 * Isolated React/Chromium regression for the public worker status cards.
 * It reuses the existing bundled React host fixture and loads the current
 * client module separately. No DPH process, provider, or settings mutation.
 */
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { readFileSync, mkdirSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const out = resolve(process.env.DPSWARM_TEST_OUTPUT || join(repo, '.tmp/worker-closeout-ui'))
mkdirSync(out, { recursive: true })
// The React-only fixture is deliberately shared from the primary checkout;
// this isolated worktree has no copied browser bundle.
const fixturePath = process.env.DPSWARM_TEST_REACT_FIXTURE || resolve(repo, '../..', '.tmp/model-settings-20260907/browser/fixture.js')
const playwrightPath = process.env.DPSWARM_TEST_PLAYWRIGHT || 'C:/Users/93711/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs'
const { chromium } = await import(pathToFileURL(resolve(playwrightPath)).href)

let fixture = readFileSync(fixturePath, 'utf8')
const marker = 'window.scope = scope;'
assert.equal(fixture.split(marker).length, 2, 'known isolated fixture hook required')
fixture = fixture.replace(marker, `${marker}
window.fixturePatch = patch => mirror.acceptView({ns:'dpswarm',revision:snapshot.revision+1,value:{...snapshot.value,...patch}});
window.workerStatus = null;`)
assert.equal(fixture.split('snapshot: null').length, 2, 'known status response hook required')
fixture = fixture.replace('snapshot: null', 'snapshot: window.workerStatus ?? null')

const css = `:root{color-scheme:dark;--dsw-alias-label-primary:#e4e8ef;--dsw-alias-label-secondary:#adb7c8;--dsw-alias-label-tertiary:#a1acbd;--dsw-alias-bg-layer-2:#222831;--dsw-alias-bg-layer-3:#1c222a;--dsw-alias-bg-module-platform:#282f39;--dsw-specific-menu:#222831;--dsw-alias-border-l2:#424b59;--dsw-alias-border-inverted:#424b59;--dsw-alias-state-warn-primary:#f0b84d;--dsw-alias-state-success-primary:#62d69b;--dsw-alias-state-error-primary:#ff7b89;--dsw-alias-brand-primary:#79a7ff;--dsw-shadow-lv3:0 8px 24px #0008}body{font-family:'Segoe UI','Microsoft YaHei',sans-serif;padding:12px;margin:0;background:#181c22;color:#e4e8ef}main{max-width:780px;margin:auto}ul{padding:0;list-style:none}aside{display:flex;gap:20px;padding:20px 0;position:relative}.dps-popCard{max-width:calc(100vw - 32px)}\n`
const html = `<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>${css}</style><main><h1>DPswarm 状态</h1><div id="settings"></div><ul id="card"></ul><aside><section>任务 A<div id="session-a"></div></section><section>任务 B<div id="session-b"></div></section></aside></main><script src="/fixture.js"></script><script src="/client.js"></script><script>mount()</script></html>`
const server = createServer((req, res) => {
  const routes = { '/': ['text/html; charset=utf-8', html], '/fixture.js': ['text/javascript', fixture], '/client.js': ['text/javascript', readFileSync(join(repo, 'dpswarm-dsh-plugin/lib/client.js'))] }
  const item = routes[req.url]; if (!item) return res.writeHead(404).end()
  res.setHeader('Content-Type', item[0]); res.end(item[1])
})
await new Promise(resolveServer => server.listen(0, '127.0.0.1', resolveServer))

const workers = [
  { session_id: 'impl', role: 'implementer', mode: 'manual', phase: 'working', observed_tokens_lower_bound: 12345, unknown_usage_calls: 1, calls_used: 3, remaining_tokens: 87655, remaining_calls: 2, candidate_count: 0 },
  { session_id: 'test', role: 'tester', mode: 'auto', phase: 'closing', observed_tokens_lower_bound: null, unknown_usage_calls: 1, calls_used: 1, remaining_tokens: 0, remaining_calls: 1, code: 'WORKER_CLOSEOUT_FINAL_ONLY', candidate_count: 1 },
  { session_id: 'review', role: 'reviewer', mode: 'unlimited', phase: 'completed', observed_tokens_lower_bound: 500, unknown_usage_calls: 0, calls_used: 1, candidate_count: 2 },
  { session_id: 'failed', role: 'implementer', mode: 'manual', phase: 'failed', observed_tokens_lower_bound: 0, unknown_usage_calls: 0, calls_used: 0, remaining_tokens: 6000, remaining_calls: 6, code: 'WORKER_TOKEN_RESERVATION_DENIED', candidate_count: 1 },
]
const checks = [], errors = [], blocked = []
let browser
try {
  browser = await chromium.launch({ headless: true })
  const page = await browser.newPage({ viewport: { width: 390, height: 940 } })
  page.setDefaultTimeout(10_000)
  page.on('pageerror', error => errors.push(error.message))
  const origin = `http://127.0.0.1:${server.address().port}`
  await page.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : (blocked.push(route.request().url()), route.abort()))
  await page.goto(origin)
  await page.evaluate(rows => {
    // The shared fixture normally wraps its status in {snapshot}; the product
    // poller consumes the actual /api/status shape, so expose that shape here.
    window.workerStatus = { bridge: { session_isolation: true }, worker_diagnostics: { available: true, workers: rows } }
    window.fetch = async (url, options) => { window.fetches.push({ url, headers: options?.headers }); return { ok: true, json: async () => window.workerStatus } }
    window.fixturePatch({ enabledSessions: ['session-a'] })
  }, workers)
  const before = await page.evaluate(() => ({ calls: window.calls.slice(), value: structuredClone(window.scope.getSnapshot().value) }))
  await page.locator('#session-a button').click()
  const status = page.getByLabel('子代理执行状态', { exact: true })
  await status.getByText('正在收尾', { exact: true }).waitFor()
  await status.getByText('需 Lead 接管', { exact: true }).waitFor()
  await status.getByText('报告已返回', { exact: true }).waitFor()
  assert.equal(await status.getByText('累计 12,345+ token（部分用量未知） · 3 次调用', { exact: true }).count(), 1)
  assert.equal(await status.getByText('累计 未知+ token（部分用量未知） · 1 次调用', { exact: true }).count(), 1)
  assert.equal(await status.getByText('剩余 87,655 token · 2 次', { exact: true }).count(), 1)
  assert.equal(await status.getByText('剩余 0 token · 1 次', { exact: true }).count(), 1)
  const rows = status.locator('.dps-workerRow')
  assert.equal(await rows.count(), 4)
  assert.match(await rows.nth(0).innerText(), /剩余 87,655 token · 2 次/)
  assert.match(await rows.nth(1).innerText(), /剩余 0 token · 1 次/)
  assert.doesNotMatch(await rows.nth(2).innerText(), /剩余/, 'unlimited worker must not present a fabricated remainder')
  assert.equal(await status.getByText('收尾阶段只提交报告', { exact: true }).count(), 1)
  assert.equal(await status.getByText('剩余 token 不足以发送下一次完整请求', { exact: true }).count(), 1)
  assert.equal(await status.getByText('已记录 1 条文件候选记录；由 Lead 核验当前文件', { exact: true }).count(), 2)
  assert.equal(await status.getByText(/候选.*完成|文件.*完成/).count(), 0, 'candidate evidence must not be presented as completion')
  checks.push('known_unknown_closing_failed_and_candidate_semantics')
  const after = await page.evaluate(() => ({ calls: window.calls.slice(), value: structuredClone(window.scope.getSnapshot().value) }))
  assert.deepEqual(after, before, 'opening diagnostics must not mutate settings')
  checks.push('diagnostics_open_is_readonly')
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false)
  const bounds = await status.boundingBox(); assert.ok(bounds && bounds.x >= 0 && bounds.width <= 390)
  await page.screenshot({ path: join(out, 'worker-diagnostics-mobile-dark.png'), fullPage: true })
  checks.push('dark_chinese_mobile_no_horizontal_overflow')
  assert.deepEqual(errors, []); assert.deepEqual(blocked, [])
  checks.push('no_page_errors_or_external_requests')
  writeFileSync(join(out, 'results.json'), JSON.stringify({ checks, transport: 'isolated React fixture plus controlled status response; no DPH, sidecar, provider, or settings write', model_calls: 0, screenshot: 'worker-diagnostics-mobile-dark.png' }, null, 2))
  console.log(`Worker diagnostics browser checks passed: ${checks.length}; ${out}`)
} finally {
  await browser?.close(); await new Promise(resolveServer => server.close(resolveServer))
}
