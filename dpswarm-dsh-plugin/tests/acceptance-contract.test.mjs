import assert from 'node:assert/strict'
import test from 'node:test'
import { parseReviewReport, renderAcceptancePrompt, REVIEW_SCHEMA, REVIEW_SCHEMA_V2, reviewReportFormat, validateReviewBindings, validateReviewRecord } from '../lib/acceptance-contract.js'

const report = () => ({ schema: REVIEW_SCHEMA, candidate_id: 'candidate-1', manifest_digest: 'a'.repeat(64), requirement_revision: 1, verdict: 'pass', requirements: [{ id: 'R1', result: 'met', evidence: ['snapshot phase measurements'] }], findings: [], unavailable_checks: [] })
const wrap = value => 'Reviewer explanation.\n```dpswarm-review-v1\n' + JSON.stringify(value) + '\n```'

test('strict review accepts a unique complete report and hashes the entire raw report', () => {
  const source = wrap(report()), parsed = parseReviewReport(source)
  assert.equal(parsed.ok, true)
  assert.deepEqual(parsed.report, report())
  assert.notEqual(parsed.report_hash, parseReviewReport('another explanation\n' + source).report_hash)
})

test('missing, duplicate, truncated and duplicate-key JSON reports never become pass', () => {
  for (const source of ['PASS', wrap(report()) + '\n' + wrap(report()), wrap(report()).slice(0, -3), wrap(report()).replace('"verdict":"pass"', '"verdict":"blocked","verdict":"pass"')]) {
    const parsed = parseReviewReport(source)
    assert.equal(parsed.ok, false, source)
    assert.equal(parsed.report, null)
    assert.ok(parsed.errors.length)
  }
})

test('invalid enum, duplicate IDs and conflicting structured body are rejected', () => {
  const invalid = report(); invalid.requirements[0].result = 'probably'
  assert.equal(parseReviewReport(wrap(invalid)).ok, false)
  const duplicate = report(); duplicate.requirements.push({ ...duplicate.requirements[0] })
  assert.equal(parseReviewReport(wrap(duplicate)).ok, false)
  assert.equal(parseReviewReport(wrap(report()) + '\n```json\n{"verdict":"needs-rework"}\n```').ok, false)
})

test('parse does not replace semantic acceptance: open finding remains present with a pass claim', () => {
  const data = report()
  data.findings = [{ id: 'F1', requirement_ids: ['R1'], paths: ['animation.html'], observation: 'Far knee bends in the opposite direction', classification: 'defect', state: 'open', evidence: ['frame at t=0.25'], reason: 'Violates cycling motion' }]
  const parsed = parseReviewReport(wrap(data))
  assert.equal(parsed.ok, true)
  assert.equal(parsed.report.findings[0].state, 'open')
})

test('prompt keeps trusted request, Lead choices, edit scope and full acceptance distinct', () => {
  const output = renderAcceptancePrompt({ user_request: 'Create a cycling animation', lead_plan: 'Far leg is correct; do not test it', edit_scope: ['near-leg'], acceptance_scope: ['both legs'], findings: [{ id: 'far-knee', observation: 'Wrong branch', classification: 'suggestion' }] })
  for (const value of ['trusted user_request', 'lead_plan', 'edit_scope', 'acceptance_scope', 'Create a cycling animation', 'Far leg is correct', 'near-leg', 'both legs', 'far-knee']) assert.ok(output.includes(value))
  assert.ok(output.includes('不能据此声称用户禁止验证'))
})


test('a valid old pass cannot be replayed without all runtime candidate bindings', () => {
  for (const field of ['candidate_id', 'manifest_digest', 'requirement_revision']) {
    const missing = report(); delete missing[field]
    assert.equal(parseReviewReport(wrap(missing)).ok, false, field)
  }
  const candidate = { candidate_id: 'current-candidate', manifest_digest: 'b'.repeat(64), requirement_revision: 3 }
  const parsed = parseReviewReport(renderAcceptancePrompt({ candidate }))
  assert.equal(parsed.ok, true)
  assert.equal(parsed.report.candidate_id, candidate.candidate_id)
  assert.equal(parsed.report.manifest_digest, candidate.manifest_digest)
  assert.equal(parsed.report.requirement_revision, candidate.requirement_revision)
})


test('discovered format contains full syntax and a current-candidate unknown template with retained observations', () => {
  const candidate = { candidate_id: 'candidate-current', manifest_digest: 'b'.repeat(64), requirement_revision: 2,
    snapshot: { consumed_manifest_refs: [{ artifact_id: 'upstream', manifest_digest: 'c'.repeat(64) }] } }
  const finding = { id: 'F1', observation: 'Earlier motion observation must remain.', requirement_ids: ['R1'],
    history: [{ decision: { paths: ['animation.html'], classification: 'unknown', state: 'open' } }] }
  const format = reviewReportFormat({ candidate, evidence_revision: 4, requirements: [{ id: 'R1', mandatory: true }], findings: [finding] })
  assert.equal(format.schema_version, REVIEW_SCHEMA)
  assert.deepEqual(format.json_schema.properties.requirements.items.properties.result.enum, ['met', 'failed', 'unknown'])
  assert.ok(format.json_schema.required.includes('evidence_revision'))
  assert.equal(format.json_schema.additionalProperties, false)
  assert.equal(format.template.verdict, 'blocked')
  assert.equal(format.template.requirements[0].result, 'unknown')
  assert.equal(format.template.findings[0].observation, finding.observation)
  assert.deepEqual(format.template.findings[0].paths, ['animation.html'])
  assert.equal(format.template.findings[0].state, 'open')
  assert.deepEqual(format.template.findings[0].evidence, [])
  const parsed = parseReviewReport(format.report_template, { final: true })
  assert.equal(parsed.ok, true, JSON.stringify(parsed.errors))
  assert.deepEqual(validateReviewBindings(parsed.report, { candidate, evidence_revision: 4, requirements: [{ id: 'R1', mandatory: true }], findings: [finding] }), [])
})

test('schema diagnostics separate invalid result, missing field and duplicate identifier with JSON paths', () => {
  const wrong = report(); wrong.requirements[0].result = 'pass'
  let issues = parseReviewReport(wrap(wrong)).errors
  const result = issues.find(row => row.path === '/requirements/0/result')
  assert.equal(result.code, 'REPORT_ENUM_INVALID')
  assert.deepEqual(result.expected, ['met', 'failed', 'unknown'])
  assert.equal(result.actual, 'pass'); assert.equal(result.schema_version, REVIEW_SCHEMA)
  assert.ok(!issues.some(row => row.code === 'REPORT_ID_DUPLICATE'))
  delete wrong.requirements[0].result
  assert.equal(parseReviewReport(wrap(wrong)).errors.find(row => row.path === '/requirements/0/result').code, 'REPORT_FIELD_REQUIRED')
  const duplicate = report(); duplicate.requirements.push({ ...duplicate.requirements[0] })
  issues = parseReviewReport(wrap(duplicate)).errors
  assert.equal(issues.find(row => row.path === '/requirements/1/id').code, 'REPORT_ID_DUPLICATE')
})

test('final syntax requires evidence revision and current binding rejects stale or omitted coverage', () => {
  const valid = report()
  assert.equal(parseReviewReport(wrap(valid), { final: true }).errors.find(row => row.path === '/evidence_revision').code, 'REPORT_FIELD_REQUIRED')
  valid.evidence_revision = 3
  const inputs = { candidate: { candidate_id: 'new-candidate', manifest_digest: 'b'.repeat(64), requirement_revision: 2 }, evidence_revision: 4,
    requirements: [{ id: 'R2', mandatory: true }], findings: [{ id: 'F1' }] }
  const errors = validateReviewBindings(valid, inputs)
  for (const path of ['/candidate_id', '/manifest_digest', '/requirement_revision', '/evidence_revision', '/requirements', '/findings']) assert.ok(errors.some(row => row.path === path), path)
})


test('staged manifest references retain complete runtime provenance and reject any binding change', () => {
  const ref = { artifact_id: 'upstream', manifest_digest: 'a'.repeat(64), worker_session_id: 'worker-1', paths: ['upstream.html'] }
  const candidate = { candidate_id: 'candidate-1', manifest_digest: 'b'.repeat(64), requirement_revision: 1, snapshot: { consumed_manifest_refs: [ref] } }
  const format = reviewReportFormat({ candidate, evidence_revision: 1, requirements: [{ id: 'R1', mandatory: true }] })
  const parsed = parseReviewReport(format.report_template, { final: true })
  assert.equal(parsed.ok, true, JSON.stringify(parsed.errors))
  const inputs = { candidate, evidence_revision: 1, requirements: [{ id: 'R1', mandatory: true }] }
  assert.deepEqual(validateReviewBindings(parsed.report, inputs), [])
  delete parsed.report.consumed_manifest_refs[0].worker_session_id
  assert.equal(validateReviewBindings(parsed.report, inputs)[0].code, 'REVIEW_DEPENDENCY_MISMATCH')
})

test('duplicate JSON keys identify their exact nested path without accepting overwritten values', () => {
  const source = wrap(report()).replace('"result":"met"', '"result":"failed","result":"met"')
  const issue = parseReviewReport(source).errors[0]
  assert.equal(issue.code, 'REPORT_DUPLICATE_KEY')
  assert.equal(issue.path, '/requirements/0/result')
  assert.equal(issue.expected, 'unique object key')
  assert.equal(issue.actual, 'result')
})

const v2report = () => ({ schema: REVIEW_SCHEMA_V2, candidate_id: 'candidate-1', manifest_digest: 'a'.repeat(64),
  requirement_revision: 1, verdict: 'pass',
  verification_plan: { revision: 1, coverage: 'Static reads cover every requirement.',
    checks: [{ id: 'check-R1', requirement_ids: ['R1'], method: 'static:read', required_for_claim: true }] },
  check_results: [{ check_id: 'check-R1', method: 'static:read', execution_status: 'completed',
    evidence_refs: ['sealed view'], limitations: [], candidate_id: 'candidate-1', manifest_digest: 'a'.repeat(64),
    requirement_revision: 1, verification_plan_revision: 1 }],
  requirements: [{ id: 'R1', result: 'met', evidence: ['sealed view'], verification_plan_revision: 1, check_refs: ['check-R1'] }],
  findings: [{ id: 'F1', requirement_ids: ['R1'], paths: [], observation: 'Optional palette.', classification: 'suggestion',
    state: 'open', evidence: [], reason: '', disposition: 'retained_suggestion', disposition_reason: 'Preference only.' }],
  unavailable_checks: [] })
const wrapV2 = value => 'Reviewer explanation.\n```dpswarm-review-v2\n' + JSON.stringify(value) + '\n```'

test('v2 reports parse behind their fence and validate plan-bound checks', () => {
  const parsed = parseReviewReport(wrapV2(v2report()))
  assert.equal(parsed.ok, true, JSON.stringify(parsed.errors))
  assert.equal(parsed.report.schema, REVIEW_SCHEMA_V2)
  // v1 fences keep working unchanged
  assert.equal(parseReviewReport(wrap(report())).ok, true)
})

test('v2 syntax rejects unknown checks, stale plan bindings and missing dispositions', () => {
  const unknown = v2report(); unknown.requirements[0].check_refs = ['check-missing']
  assert.ok(validateReviewRecord(unknown).some(e => e.code === 'REPORT_FIELD_INVALID' || e.path.includes('check_refs')))
  const stale = v2report(); stale.check_results[0].verification_plan_revision = 2
  assert.ok(validateReviewRecord(stale).some(e => e.code === 'VERIFICATION_PLAN_STALE'))
  const undisposed = v2report(); delete undisposed.findings[0].disposition
  assert.ok(validateReviewRecord(undisposed).some(e => e.path === '/findings/0/disposition'))
  const unexplained = v2report(); unexplained.findings[0].disposition_reason = ''
  assert.ok(validateReviewRecord(unexplained).some(e => e.path === '/findings/0/disposition_reason'))
  // Mixed fences never parse.
  assert.equal(parseReviewReport(wrap(report()) + wrapV2(v2report())).ok, false)
})

test('v2 format scaffolds a plan, check results and dispositions; v1 format unchanged', () => {
  const v2 = reviewReportFormat({ candidate: { candidate_id: 'c1', manifest_digest: 'b'.repeat(64), requirement_revision: 2 },
    evidence_revision: 3, requirements: [{ id: 'R1', description: 'x', mandatory: true }], findings: [], reviewContract: REVIEW_SCHEMA_V2 })
  assert.equal(v2.schema_version, REVIEW_SCHEMA_V2)
  assert.equal(v2.template.verification_plan.checks[0].id, 'check-R1')
  assert.equal(v2.template.requirements[0].check_refs[0], 'check-R1')
  assert.equal(v2.template.check_results[0].candidate_id, 'c1')
  const v1 = reviewReportFormat({ candidate: null, evidence_revision: null, requirements: [], findings: [] })
  assert.equal(v1.schema_version, REVIEW_SCHEMA)
  assert.ok(!('verification_plan' in v1.template))
})


test('report diagnostics identify the submitted schema and use fence version only when JSON cannot be parsed', () => {
  for (const schema of [REVIEW_SCHEMA, REVIEW_SCHEMA_V2]) {
    const value = schema === REVIEW_SCHEMA ? report() : v2report()
    const missingField = schema === REVIEW_SCHEMA ? 'unavailable_checks' : 'verification_plan'
    delete value[missingField]
    const wrapCurrent = raw => '```' + schema + '\n' + raw + '\n```'
    const malformed = parseReviewReport(wrapCurrent(JSON.stringify(value)))
    assert.equal(malformed.ok, false)
    assert.equal(malformed.schema_version, schema)
    assert.ok(malformed.errors.every(issue => issue.schema_version === schema))
    assert.ok(malformed.errors.some(issue => issue.path === '/' + missingField))
    const invalidJson = parseReviewReport(wrapCurrent('{'))
    assert.equal(invalidJson.ok, false)
    assert.equal(invalidJson.schema_version, schema)
    assert.ok(invalidJson.errors.every(issue => issue.schema_version === schema))
    const bindingErrors = validateReviewBindings(schema === REVIEW_SCHEMA ? report() : v2report(), {
      candidate: { candidate_id: 'different-candidate' }, evidence_revision: 10 })
    assert.ok(bindingErrors.length)
    assert.ok(bindingErrors.every(issue => issue.schema_version === schema))
  }
})


for (const fenceVersion of [REVIEW_SCHEMA, REVIEW_SCHEMA_V2]) {
  for (const declaredSchema of [undefined, 'dpswarm-review-future']) {
    test(`${fenceVersion} fence with ${declaredSchema === undefined ? 'missing' : 'unknown'} JSON schema refuses version selection without foreign field advice`, () => {
      const value = { verdict: 'pass', ...(declaredSchema === undefined ? {} : { schema: declaredSchema }) }
      const source = '```' + fenceVersion + '\n' + JSON.stringify(value) + '\n```'
      const parsed = parseReviewReport(source, { final: true })
      assert.equal(parsed.ok, false)
      assert.equal(parsed.report, null)
      assert.equal(parsed.transport_schema_version, fenceVersion)
      assert.equal(parsed.schema_version, null, 'The fence is not a validated report schema')
      assert.equal(parsed.errors.length, 1, 'Do not continue into either version-specific field validator')
      const [error] = parsed.errors
      assert.equal(error.code, 'REPORT_SCHEMA_VERSION_INVALID')
      assert.equal(error.path, '/schema')
      assert.equal(error.schema_version, null)
      assert.deepEqual(error.expected, [REVIEW_SCHEMA, REVIEW_SCHEMA_V2])
      assert.equal(error.actual, declaredSchema ?? null)
    })
  }
}
