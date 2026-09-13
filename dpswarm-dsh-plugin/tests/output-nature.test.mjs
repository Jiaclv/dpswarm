import assert from 'node:assert/strict'
import test from 'node:test'
import { pseudoToolCallMarkup } from '../lib/output-nature.js'

// Trimmed verbatim marker regions from the pelican-014 live run (native.zip,
// 2026-09-12). All three parked workers ended with this markup instead of a
// report; the calls in it never executed.
const LIVE_TESTER = `The math extraction hit a PowerShell scoping bug; now the decisive kinematic check plus a real browser run.

<｜｜DSML｜｜ calls>
<｜｜DSML｜｜ invoke name="pwsh">
<｜｜DSML｜｜ parameter name="command" string="true">
$ErrorActionPreference='Continue'
$view='K:\\秋招\\pelican-bicycle.html'
</｜｜DSML｜｜ parameter>
<｜｜DSML｜｜ parameter name="description" string="true">Verify leg IK rigidity and pedal attachment</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>`

const LIVE_IMPLEMENTER = `Element balance confirmed 33/33 and the ankle-targeted IK is now rigid. Applying the corrected leg paths:

<｜｜DSML｜｜ calls>
<｜｜DSML｜｜ invoke name="edit">
<｜｜DSML｜｜ parameter name="file_path" string="true">K:\\秋招\\pelican-bicycle.html</｜｜DSML｜｜ parameter>
<｜｜DSML｜｜ parameter name="old_string" string="true">d="M 380 224 L 453.4 291.9 L 383 365"</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>`

const LIVE_REVIEWER = `Browser render was denied by the host sandbox (Chrome mojo IPC: ACCESS_DENIED 0x5). Trying one alternative engine path.

<｜｜DSML｜｜ calls>
<｜｜DSML｜｜ invoke name="pwsh">
<｜｜DSML｜｜ parameter name="command" string="true">Get-ChildItem .</｜｜DSML｜｜ parameter>
<｜｜DSML｜｜ parameter name="timeoutMs" string="false">300000</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>`

const PROVIDER_TOKENS = '<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>function<｜tool▁sep｜>pwsh<｜tool▁sep｜>```json\n{"command": "ls"}\n```<｜tool▁call▁end｜><｜tool▁calls▁end｜>'

test('live-run DSML pseudo calls are detected with their invoked tool names', () => {
  for (const [name, sample, tool] of [['tester', LIVE_TESTER, 'pwsh'], ['implementer', LIVE_IMPLEMENTER, 'edit'], ['reviewer', LIVE_REVIEWER, 'pwsh']]) {
    const found = pseudoToolCallMarkup(sample)
    assert.ok(found, name)
    assert.deepEqual(found.families, ['dsml-markup'])
    assert.deepEqual(found.tool_names, [tool])
    assert.ok(found.marker_count >= 6, `${name} marker_count ${found.marker_count}`)
  }
})

test('provider special-token tool markup is detected as its own family', () => {
  const found = pseudoToolCallMarkup(PROVIDER_TOKENS)
  assert.deepEqual(found.families, ['provider-special-tokens'])
  assert.deepEqual(found.tool_names, [])
  assert.equal(found.marker_count, 6)
})

test('plain prose, a single valid review fence and empty input stay clean', () => {
  assert.equal(pseudoToolCallMarkup('实现报告。一切都是文本。'), null)
  assert.equal(pseudoToolCallMarkup('```dpswarm-review-v1\n{"schema":"dpswarm-review-v1","verdict":"pass"}\n```'), null)
  assert.equal(pseudoToolCallMarkup(''), null)
  assert.equal(pseudoToolCallMarkup(null), null)
  assert.equal(pseudoToolCallMarkup(undefined), null)
  // The bare word without tag framing is not markup.
  assert.equal(pseudoToolCallMarkup('The model emitted DSML instead of a report.'), null)
})

test('a valid fence combined with pseudo markup is still flagged (advisory)', () => {
  const mixed = '```dpswarm-review-v1\n{"schema":"dpswarm-review-v1","verdict":"blocked"}\n```\n\n' + LIVE_IMPLEMENTER
  const found = pseudoToolCallMarkup(mixed)
  assert.deepEqual(found.families, ['dsml-markup'])
  assert.deepEqual(found.tool_names, ['edit'])
})

test('markup quoted inside a code fence is still flagged (documented advisory behavior)', () => {
  const quoted = '上次失败输出如下：\n```text\n' + LIVE_TESTER + '\n```'
  assert.ok(pseudoToolCallMarkup(quoted))
})

test('multiple invokes list every distinct tool name once', () => {
  const two = '<｜｜DSML｜｜ invoke name="pwsh">x</｜｜DSML｜｜ invoke>\n<｜｜DSML｜｜ invoke name="pwsh">y</｜｜DSML｜｜ invoke>\n<｜｜DSML｜｜ invoke name="read">z</｜｜DSML｜｜ invoke>'
  const found = pseudoToolCallMarkup(two)
  assert.deepEqual(found.tool_names, ['pwsh', 'read'])
})
