import assert from 'node:assert/strict'
import test from 'node:test'
import { AcceptanceRuntime } from '../lib/acceptance-runtime.js'

// record() turns an unparseable tester/reviewer delivery into durable evidence
// plus a Lead-facing report_error. These tests pin that the error now also
// states the output nature: DSML pseudo tool-call markup is flagged as
// never-executed, while plain invalid prose stays unflagged.

const DSML_FINAL = `Now the decisive kinematic check plus a real browser run.

<｜｜DSML｜｜ calls>
<｜｜DSML｜｜ invoke name="pwsh">
<｜｜DSML｜｜ parameter name="command" string="true">Get-ChildItem .</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>`

function fixture() {
  const posts = []
  const state = { acceptance: { id: 'contract', revision: 1, evidence_revision: 3,
      candidate: { id: 'cand-1', candidate_id: 'cand-1', roster_evidence: {} },
      contract: { evidence: {} } },
    sidecar: { async call(method, path, request) {
      if (path === '/api/status') return { snapshot: {
        work_items: { 'item-t': { kind: 'derive', acceptance: 'submitted',
          submission_package_id: 'pkg-t', submission_session_id: 'sess-t' } },
        nodes: { 'node-t': { id: 'node-t', item: 'item-t', execution_session_id: 'sess-t' } } } }
      assert.equal(path, '/api/acceptance')
      posts.push(structuredClone(request))
      return { ok: true, contract_id: 'contract', revision: ++state.acceptance.revision,
        evidence_revision: state.acceptance.evidence_revision + 1,
        contract: state.acceptance.contract, evidence_id: 'evidence-' + posts.length }
    } } }
  return { state, posts, runtime: new AcceptanceRuntime({}) }
}

test('a DSML pseudo-call delivery is flagged as never-executed in report_error', async () => {
  const { state, posts, runtime } = fixture()
  const result = { deliveries: [{ item_id: 'item-t', output: DSML_FINAL }] }
  const saved = await runtime.record(state, null, 'tester', result)
  assert.ok(saved.ok)
  // The invalid report remains durable evidence; the evidence payload itself
  // keeps the raw report only (no JS-side classification leaks into it).
  assert.equal(posts[0].action, 'evidence')
  assert.equal(posts[0].payload.report, DSML_FINAL)
  const error = state.acceptance.report_error
  assert.equal(error.item_id, 'item-t')
  assert.equal(error.role, 'tester')
  assert.equal(error.errors[0].code, 'REPORT_BLOCK_INVALID')
  assert.deepEqual(error.output_nature.families, ['dsml-markup'])
  assert.deepEqual(error.output_nature.tool_names, ['pwsh'])
  assert.match(error.guidance, /never executed/)
  assert.match(error.guidance, /dpswarm_repair_report/)
})

test('plain invalid prose keeps report_error without a markup flag', async () => {
  const { state, runtime } = fixture()
  const result = { deliveries: [{ item_id: 'item-t', output: 'VERDICT: pass\n一切正常，但没有结构报告。' }] }
  await runtime.record(state, null, 'reviewer', result)
  const error = state.acceptance.report_error
  assert.equal(error.errors[0].code, 'REPORT_BLOCK_INVALID')
  assert.equal(error.output_nature, null)
  assert.equal(error.guidance, undefined)
})
