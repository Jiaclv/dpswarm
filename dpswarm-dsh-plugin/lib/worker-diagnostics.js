// Reads trusted native session events only. Never opens candidate paths, parses
// shell commands, or treats model prose as evidence of a completed file write.
const copy = value => value == null ? null : JSON.parse(JSON.stringify(value))
const text = value => typeof value === 'string' && value.length > 0
const budgetCodes = new Map([
  ['WORKER_TOKEN_RESERVATION_DENIED', ['budget', 'admission']],
  ['WORKER_TOKEN_LIMIT_REACHED', ['budget', 'exhausted']],
  ['WORKER_CALL_LIMIT_REACHED', ['call_limit', 'exhausted']],
])
const providerCodes = new Set(['AUTH', 'RATE_LIMIT', 'CONTEXT_WINDOW_EXCEEDED', 'NETWORK',
  'TIMEOUT', 'SERVICE_UNAVAILABLE', 'SERVER', 'SERVER_ERROR', 'HTTP_ERROR', 'INVALID_REQUEST', 'BAD_REQUEST', 'NO_ADAPTER', 'MODEL_NOT_FOUND'])

function knownBudgetCode(error) {
  if (budgetCodes.has(error?.code)) return error.code
  // DSH flattens non-LlmError instances to UNKNOWN + errorChain(message).
  // Match only an exact known token (or its conventional ': ' prefix), never
  // search arbitrary text, worker output, tool results, or previous denials.
  if (error?.code === 'UNKNOWN' && typeof error.message === 'string') {
    return [...budgetCodes.keys()].find(code => error.message === code || error.message.startsWith(code + ': '))
  }
}

export function classifyWorkerFailure({ nativeTerminal, result, error, signal }) {
  const reason = nativeTerminal?.reason, native = reason?.kind === 'error' ? reason.error : null
  const raw = native || (error ? { code: error.code || null, message: String(error.message ?? error) } : null)
  const make = (code, category, source, phase) => ({ code, category, source, ...(phase ? { phase } : {}),
    message: raw?.message || code, raw_code: raw?.code || null, raw_message: raw?.message || null })
  const code = knownBudgetCode(native) || knownBudgetCode(error)
  if (code) return make(code, budgetCodes.get(code)[0], native ? 'native-terminal' : 'runtime-error', budgetCodes.get(code)[1])
  if (native) return make(native.code && native.code !== 'UNKNOWN' ? native.code : 'SUBAGENT_NOT_COMPLETED',
    providerCodes.has(native.code) || native.provider || native.status != null ? 'provider' : 'unknown', 'native-terminal')
  if (signal?.aborted && signal.reason?.code === 'WORKER_TIMEOUT') {
    return { ...make('WORKER_TIMEOUT', 'timeout', 'fixed-role-timer'), message: String(signal.reason.message),
      cancellation_reason: { code: signal.reason.code, message: String(signal.reason.message) } }
  }
  if (reason?.kind === 'aborted' || signal?.aborted || result?.stopReason === 'aborted') {
    const cause = reason?.reason?.kind || signal?.reason?.kind
    // A parent cancellation is not proof the user clicked Stop. Preserve the
    // exact host cause and any typed caller cause, rather than inventing intent.
    const cancellationCode = cause === 'user' ? 'WORKER_USER_CANCELLED'
      : cause === 'parent' ? 'WORKER_PARENT_CANCELLED' : 'SUBAGENT_ABORTED'
    return { ...make(cancellationCode, 'cancelled', reason ? 'native-terminal' : 'caller-signal'),
      cancellation_reason: copy(reason?.reason) || (signal?.reason ? {
        kind: signal.reason.kind || null, code: signal.reason.code || null, message: String(signal.reason.message || signal.reason),
      } : null) }
  }
  if (reason?.kind === 'max-tokens' || result?.stopReason === 'max-tokens') {
    return make('WORKER_OUTPUT_LIMIT_REACHED', 'output_limit', reason ? 'native-terminal' : 'native-result')
  }
  if (error) return make(error.code || 'SUBAGENT_EXECUTION_FAILED',
    error.code === 'SUBAGENT_DISPOSAL_FAILED' ? 'cleanup' : providerCodes.has(error.code) ? 'provider' : 'unknown', 'runtime-error')
  return null
}

function sessionEvidence(session, sessionId, rootId) {
  if (!session) return { events: [], available: false, issue: 'NATIVE_SESSION_UNAVAILABLE' }
  const header = session.header
  if (session.id !== sessionId || header?.id !== undefined && header.id !== sessionId
      || header?.parentSession !== rootId || header?.origin !== 'subagent' || header?.delegationDepth !== 1) {
    return { events: [], available: false, issue: 'NATIVE_SESSION_IDENTITY_MISMATCH' }
  }
  const seed = header.seedLength ?? 0
  if (!Array.isArray(session.events) || !Number.isSafeInteger(seed) || seed < 0 || seed > session.events.length) {
    return { events: [], available: false, issue: 'NATIVE_SESSION_EVENTS_INVALID' }
  }
  return { events: session.events.slice(seed), available: true, issue: null }
}

function candidatesFrom(events) {
  const calls = new Map(), completed = new Set(), candidates = []
  let untracked = false
  for (const event of events) {
    if (event.type === 'tool/code-dispatch' || event.type === 'tool/code-dispatch-start') untracked = true
    // Legacy events omit surfaceOp; current append is the string 'append'.
    // Replacement is an object {start,end}, never a fresh execution.
    if (event.surfaceOp !== undefined && event.surfaceOp !== 'append') continue
    if (event.type === 'tool/call') {
      const call = event.data
      if (!['write', 'edit'].includes(call?.name)) { untracked = true; continue }
      let args
      try { args = JSON.parse(call.arguments) } catch { continue }
      if (text(call.callId) && text(args?.file_path)) calls.set(call.callId, { event, path: args.file_path })
    } else if (event.type === 'tool/result') {
      const data = event.data, message = data?.message
      // Native ToolResultMessage is a user-role wrapper around exactly one
      // tool-result block; call identity is on source and repeated in the block.
      const callId = message?.source?.kind === 'tool' ? message.source.callId : null
      const blocks = message?.content?.filter(b => b?.type === 'tool-result') || []
      const result = blocks.length === 1 && blocks[0].toolCallId === callId ? blocks[0] : null
      const call = calls.get(callId)
      if (!result) { untracked = true; continue }
      if (!call || completed.has(callId) || result.isError !== false || data.error
          || data.turn !== call.event.data.turn || data.step !== call.event.data.step) continue
      completed.add(callId)
      candidates.push({ path: call.path, operation: call.event.data.name === 'write' ? 'write' : 'edit',
        tool_name: call.event.data.name, call_id: callId, call_seq: call.event.seq,
        result_seq: event.seq, at: event.time ?? null, source: 'native-successful-tool',
        path_kind: 'tool-argument; resolved target not re-opened', verification: 'unverified',
        current_file_state: 'unknown' })
    }
  }
  return { candidates, untracked }
}

export async function workerDiagnostics({ session, sessionId, rootId, role, result, error, signal,
  cleanup, budget, evidenceError }) {
  const evidence = sessionEvidence(session, sessionId, rootId), events = evidence.events
  const end = events.findLast(e => e.type === 'turn/end')
  const nativeTerminal = end ? { reason: copy(end.data.reason), seq: end.seq, at: end.time ?? null } : null
  const output = Array.isArray(result?.output) ? result.output.filter(b => b?.type === 'text' && typeof b.text === 'string').map(b => b.text).join('\n') : ''
  const message = events.findLast(e => e.type === 'assistant/message'
    && e.data?.message?.content?.some(b => b.type === 'text' && text(b.text)))
  const recoverable = message?.data.message.content.filter(b => b.type === 'text').map(b => b.text).join('\n') || ''
  const report = output.trim() ? { text: output, source: 'native-result' } : recoverable.trim()
    ? { text: recoverable, source: 'native-assistant-message', event_seq: message.seq, interrupted: message.data.interrupted === true } : null
  let budgetSnapshot = null, budgetError = null
  try {
    budgetSnapshot = await budget?.diagnosticsForSession?.(sessionId) || null
    if (budgetSnapshot && (budgetSnapshot.worker_session_id !== sessionId || budgetSnapshot.root_session_id !== rootId)) {
      budgetSnapshot = null; budgetError = 'WORKER_BUDGET_IDENTITY_MISMATCH'
    }
  } catch (failure) { budgetError = String(failure?.message ?? failure) }
  const files = candidatesFrom(events)
  const failure = classifyWorkerFailure({ nativeTerminal, result, error, signal })
  const completed = !error && !failure && result?.stopReason === 'completed' && cleanup?.physical_cleanup_confirmed === true
  return { version: 'worker-diagnostic-v1', role: role || 'worker', worker_session_id: sessionId, root_session_id: rootId,
    native_stop_reason: result?.stopReason || null, native_terminal: nativeTerminal, failure,
    budget: copy(budgetSnapshot), evidence: { native_session_available: evidence.available,
      native_terminal_available: nativeTerminal !== null, error: evidenceError || evidence.issue, budget_error: budgetError },
    closeout: { completion: completed ? 'completed' : report || files.candidates.length ? 'partial' : 'failed',
      report_available: report !== null, report_source: report?.source || null, report,
      requires_lead_verification: true, candidates: files.candidates,
      // Exact write/edit only; shell, Code Mode, external tools or a missing log
      // can write too. An empty list never proves there is no recoverable file.
      candidate_evidence_complete: evidence.available && !files.untracked,
      candidate_scope: 'successful native write/edit calls only; no filesystem scan',
      budget_closeout: copy(budgetSnapshot?.closeout) }, cleanup: copy(cleanup) }
}


// Model-facing views are deliberately small. Full terminal events, all call
// receipts, report text and candidate paths remain in the authenticated audit.
const excerpt = (value, limit = 240) => typeof value === 'string'
  ? value.length > limit ? value.slice(0, limit) + '… [truncated; full value in plugin audit]' : value
  : value
const fields = (value, names) => value && typeof value === 'object'
  ? Object.fromEntries(names.filter(k => value[k] !== undefined).map(k => [k, excerpt(value[k])])) : null
const budgetFields = ['worker_session_id', 'root_session_id', 'mode', 'tokenLimit', 'callLimit', 'phase', 'frozen',
  'calls', 'observed_tokens_lower_bound', 'committed_tokens', 'unknown_usage_calls', 'active_calls',
  'remaining_tokens', 'remaining_calls', 'error']
const denialFields = ['code', 'stage', 'input_estimate', 'output_limit', 'required_reservation', 'remaining_tokens', 'remaining_calls', 'at']

export function compactBudgetStatus(value) {
  if (!value || typeof value !== 'object') return value || null
  if (Array.isArray(value.children)) return { scope: value.scope, lead_limited: value.lead_limited,
    configured: fields(value.configured, ['mode', 'tokenLimit', 'callLimit']),
    children: value.children.slice(-6).map(compactBudgetStatus), children_total: value.children.length,
    children_omitted: Math.max(0, value.children.length - 6), coverage: value.coverage,
    detail_source: 'authenticated /api/plugin-audit; per-call receipts omitted from model view' }
  return { ...fields(value, budgetFields),
    decision: fields(value.decision, ['tokenLimit', 'callLimit', 'reason']),
    policy_binding: fields(value.policy_binding, ['authority', 'run_id', 'label', 'subtask']),
    closeout: fields(value.closeout, ['mode', 'trigger', 'at', 'remaining_tokens', 'remaining_calls']),
    last_denial: fields(value.last_denial, denialFields) }
}

export function compactWorkerDiagnostic(value) {
  if (!value || typeof value !== 'object') return null
  const closeout = value.closeout || {}, candidates = Array.isArray(closeout.candidates) ? closeout.candidates : []
  const report = closeout.report
  return { version: value.version, role: value.role, worker_session_id: value.worker_session_id,
    native_stop_reason: value.native_stop_reason,
    native_terminal: value.native_terminal ? { seq: value.native_terminal.seq, at: value.native_terminal.at,
      reason: { kind: value.native_terminal.reason?.kind,
        error: fields(value.native_terminal.reason?.error, ['code', 'message']),
        reason: fields(value.native_terminal.reason?.reason, ['kind']) } } : null,
    failure: fields(value.failure, ['code', 'category', 'source', 'phase', 'raw_code', 'message']),
    budget: compactBudgetStatus(value.budget),
    closeout: { completion: closeout.completion, report_available: closeout.report_available,
      report_source: closeout.report_source, report: report ? { text: excerpt(report.text, 600),
        original_chars: report.text?.length || 0, truncated: (report.text?.length || 0) > 600,
        source: report.source, interrupted: report.interrupted } : null,
      requires_lead_verification: true, candidate_count: candidates.length,
      candidates: candidates.slice(0, 3).map(c => ({ path: excerpt(c.path, 400), path_truncated: (c.path?.length || 0) > 400,
        operation: c.operation, at: c.at, result_seq: c.result_seq, source: c.source, verification: 'unverified' })),
      candidates_omitted: Math.max(0, candidates.length - 3), candidate_evidence_complete: closeout.candidate_evidence_complete },
    cleanup: fields(value.cleanup, ['physical_cleanup_confirmed', 'code', 'message']),
    evidence: fields(value.evidence, ['native_session_available', 'native_terminal_available', 'error', 'budget_error']),
    audit_error: fields(value.audit_error, ['code', 'message']) }
}

export function compactWorkerEntry(value) {
  const result = { ...fields(value, ['item_id', 'title', 'kind', 'role', 'level', 'stop_reason', 'execution_session_id',
    'code', 'error', 'admission_stage', 'evidence_kind', 'subtask', 'subtask_index']), diagnostic: compactWorkerDiagnostic(value.diagnostic) }
  if (typeof value.output === 'string') {
    result.output = excerpt(value.output, 600); result.output_original_chars = value.output.length
    result.output_truncated = value.output.length > 600
    result.detail_source = 'Full report: dpswarm_report(item_id, offset, limit) pages the unabridged text; authenticated /api/plugin-audit remains the audit of record. Inspect original task and files before deciding'
    // Avoid emitting the exact same report twice in a single role entry.
    if (result.diagnostic?.closeout) delete result.diagnostic.closeout.report
  }
  if (value.token_usage) result.token_usage = fields(value.token_usage, ['input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'cost_usd'])
  if (value.details) result.details = fields(value.details, ['sessionId', 'stopReason', 'published', 'physicalCleanupConfirmed', 'disposalError'])
  if (value.control_settlement) result.control_settlement = fields(value.control_settlement, ['ok', 'outcome', 'error'])
  return result
}

export function compactDiagnosticRecords(records) {
  return records.slice(-6).map(row => ({ item_id: row.item_id, role: row.role,
    worker_session_id: row.worker_session_id || row.execution_session_id,
    diagnostic: compactWorkerDiagnostic(row.diagnostic),
    detail_source: 'dpswarm_report(item_id) pages the full report; authenticated /api/plugin-audit keeps the complete record' }))
}
