/** Browser regression for legacy RPC and DSH 0.1.5 typed Remote.
 * Supply DPSWARM_TEST_PLAYWRIGHT and DPSWARM_TEST_REACT_FIXTURE (the React
 * fixture emitted by client-browser.mjs). All settings/catalogs are isolated.
 */
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { readFileSync, mkdirSync, writeFileSync } from 'node:fs'
import { resolve, join } from 'node:path'
import { pathToFileURL } from 'node:url'
const { chromium } = await import(pathToFileURL(process.env.DPSWARM_TEST_PLAYWRIGHT).href)
const fixture = readFileSync(process.env.DPSWARM_TEST_REACT_FIXTURE)
const client = readFileSync(new URL('../lib/client.js', import.meta.url))
const out = resolve(process.env.DPSWARM_TEST_OUTPUT || '.tmp/plugin-startup-20260911')
mkdirSync(out, { recursive: true })
const html = `<!doctype html><meta charset="utf-8"><div id="settings"></div><ul id="card"></ul><div id="session-a"></div><div id="session-b"></div>
<script src="/fixture.js"></script><script src="/client.js"></script><script>
const originalApply = dpsModule.apply;
dpsModule.apply = ctx => {
  window.remoteCalls = [];
  if (location.search.includes('remote')) {
    const legacy = ctx.connection.api;
    delete ctx.connection.api;
    // DSH 0.1.5 exposes each Remote namespace as its own service, reached
    // through ctx.get(); there is no connection.api carrier any more.
    const namespaces = {
      'remote.settings': {
        async mutate(ns, ops, expectedRevision) {
          if (typeof ns !== 'string' || !Array.isArray(ops) || !Number.isInteger(expectedRevision)) throw new Error('Invalid Remote mutation arguments');
          remoteCalls.push('settings.mutate');
          return (await legacy.settings.mutate({ ns, ops, expectedRevision })).result;
        },
        async describe(...args) {
          if (args.length) throw new Error('Remote describe takes no arguments');
          remoteCalls.push('settings.describe');
          return (await legacy.settings.describe({})).result;
        }
      },
      'remote.session': { async modelCatalog(...args) {
        if (args.length) throw new Error('Remote modelCatalog takes no arguments');
        remoteCalls.push('session.modelCatalog');
        return (await legacy.llm.models({})).result;
      } }
    };
    ctx.get = name => namespaces[name];
  }
  if (location.search.includes('readonly')) ctx.connection.isLoopback = false;
  return originalApply(ctx);
};
mount();
</script>`
const server = createServer((req, res) => {
  const route = req.url.split('?')[0]
  res.setHeader('Content-Type', route === '/' ? 'text/html; charset=utf-8' : 'text/javascript')
  res.end(route === '/fixture.js' ? fixture : route === '/client.js' ? client : html)
})
await new Promise(r => server.listen(0, '127.0.0.1', r))
const browser = await chromium.launch({ headless: true })
const results = []
try {
  for (const mode of ['legacy', 'remote', 'remote-readonly']) {
    const page = await browser.newPage({ viewport: { width: 1280, height: 950 } })
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    page.setDefaultTimeout(8000)
    await page.goto(`http://127.0.0.1:${server.address().port}/?${mode}`)
    const form = page.getByRole('form', { name: 'CM配置', exact: true })
    await form.waitFor()
    assert.equal(await page.evaluate(() => registrations.length), 3)
    assert.deepEqual(await page.evaluate(() => calls), [])
    await form.getByRole('button', { name: '选择CM模型', exact: true }).click()
    await page.getByRole('combobox', { name: '搜索模型或 Provider' }).fill('coding-plan')
    await page.getByRole('dialog').getByRole('option').first().click()
    assert.equal(await page.evaluate(() => scope.getSnapshot().value.cmModel), 'deepseek-v4-flash')
    if (mode.endsWith('readonly')) {
      await form.getByRole('button', { name: '保存CM配置', exact: true }).click()
      await form.getByRole('alert').filter({ hasText: '宿主设置当前不可写' }).waitFor()
      assert.deepEqual(await page.evaluate(() => calls), [])
    } else {
      await page.evaluate(() => { window.failNext = true })
      await form.getByRole('button', { name: '保存CM配置', exact: true }).click()
      await form.getByRole('alert').filter({ hasText: 'fixture host write rejected' }).waitFor()
      assert.equal(await page.evaluate(() => scope.getSnapshot().value.cmModel), 'deepseek-v4-flash')
      await form.getByRole('button', { name: '保存CM配置', exact: true }).click()
      await page.waitForFunction(() => scope.getSnapshot().value.cmModel === 'glm-5.3-flash')
      assert.equal(await page.evaluate(() => scope.getSnapshot().value.cmProvider), 'coding-plan')
      assert.deepEqual(await page.evaluate(() => scope.getSnapshot().value.enabledSessions), [])
      assert.deepEqual(await page.evaluate(() => scope.getSnapshot().value.cmEnabledSessions), [])
      const request = await page.evaluate(() => calls.at(-1))
      assert.equal(request.ns, 'dpswarm')
      assert.equal(request.expectedRevision, 1)
      assert.ok(request.ops.some(op => op.path[0] === 'cmModel' && op.value === 'glm-5.3-flash'))
      if (mode === 'remote') {
        const methods = await page.evaluate(() => remoteCalls)
        for (const name of ['session.modelCatalog', 'settings.describe', 'settings.mutate']) assert.ok(methods.includes(name))
      }
    }
    assert.deepEqual(errors, [])
    await page.screenshot({ path: join(out, `${mode}.png`), fullPage: true })
    results.push({ mode, passed: true, page_errors: errors })
    await page.close()
  }
  writeFileSync(join(out, 'browser-results.json'), JSON.stringify({ results, external_model_calls: 0, settings: 'isolated fixtures only' }, null, 2))
  console.log(JSON.stringify(results))
} finally { await browser.close(); await new Promise(r => server.close(r)) }
