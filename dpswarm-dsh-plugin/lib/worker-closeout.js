// A forecast inside the existing worker grant, never an additional allocation.
export const CLOSEOUT_MARKER = 'DPSWARM_WORKER_FINAL_ONLY'
export const CLOSEOUT_OUTPUT_RESERVE = 2048
// Below this output size the next step cannot write a full report or a
// meaningful file edit. Limits are anomaly RAILS, not per-task plans: parking
// early hands the decision to the Lead instead of silently squeezing outputs.
export const CLOSEOUT_REPORT_FLOOR = 4096
// The park forecast runs on estimates; the hard rail runs on the measured
// envelope. Where the two disagree (tokenizer skew, history growth), the
// estimate always said "fits" — the 00:14 implementer died exactly in that
// gap, one step after saving its file, with no chance to report. Slack of 1/3
// on the estimated pair parks such workers one step earlier; over-parking
// costs only an earlier report, under-parking costs the report entirely.
export const CLOSEOUT_ESTIMATE_SLACK_RATIO = 1 / 3
export const CLOSEOUT_INSTRUCTION = `[${CLOSEOUT_MARKER}]
This worker reached its budget rail. Stop expanding the task and return your final report now: what is complete, which files were provably saved (exact paths), what remains unfinished, and an estimate of what is left. If nothing was provably saved, say so explicitly. Tool calls are disabled and will be rejected — write the report as plain prose, never as tool-call markup. The Lead will decide whether to continue the work in a linked continuation, accept the partial result, or stop. Do not claim unperformed tests, successful delivery, or completion without evidence. Do not ask for more budget or wait for another step. This instruction does not change the task or increase your allowance.`

// Check the frozen request that will actually reach the adapter. DSH 0.1.5
// projects its prompt into system-role messages; legacy one-shot callers use
// options.system. A later nonempty system prompt supersedes an earlier one.
// User/tool text must never satisfy the closeout instruction requirement.
export function requestSystemText(options) {
  let current = typeof options?.system === 'string' ? options.system : ''
  for (const message of Array.isArray(options?.messages) ? options.messages : []) {
    if (message?.role !== 'system') continue
    const content = message.content
    const text = typeof content === 'string' ? content : Array.isArray(content)
      ? content.filter(part => part?.type === 'text' && typeof part.text === 'string').map(part => part.text).join('\n') : ''
    if (text.trim()) current = text
  }
  return current
}

export function closeoutForecast({ remainingTokens, remainingCalls, inputEstimate, finalInputEstimate }) {
  // Rail model: park when one more full step plus a viable report no longer
  // fit, then hand the continuation decision to the Lead. No output squeeze.
  const slack = Math.ceil((inputEstimate + finalInputEstimate) * CLOSEOUT_ESTIMATE_SLACK_RATIO)
  const park = remainingTokens < inputEstimate + finalInputEstimate + slack + CLOSEOUT_REPORT_FLOOR + CLOSEOUT_OUTPUT_RESERVE
  return {
    final_only: remainingCalls <= 1 || park,
    trigger: remainingCalls <= 1 ? 'last_available_call' : 'budget_rail',
    input_estimate: inputEstimate, final_input_estimate: finalInputEstimate,
    estimate_slack: slack,
    next_input_reserve: Math.max(inputEstimate, finalInputEstimate),
    report_floor: CLOSEOUT_REPORT_FLOOR, delivery_output_reserve: CLOSEOUT_OUTPUT_RESERVE,
    forecast_limitations: 'Estimated full input plus a 1/3 estimate-uncertainty slack, a report floor and a short delivery reserve; provider tokenization and mid-step history growth can still exceed this forecast. Hard admission remains authoritative.',
  }
}
