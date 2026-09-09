// A forecast inside the existing worker grant, never an additional allocation.
export const CLOSEOUT_MARKER = 'DPSWARM_WORKER_FINAL_ONLY'
export const CLOSEOUT_OUTPUT_RESERVE = 2048
// Below this output size the next step cannot write a full report or a
// meaningful file edit. Limits are anomaly RAILS, not per-task plans: parking
// early hands the decision to the Lead instead of silently squeezing outputs.
export const CLOSEOUT_REPORT_FLOOR = 4096
export const CLOSEOUT_INSTRUCTION = `[${CLOSEOUT_MARKER}]
This worker reached its budget rail. Stop expanding the task and return your final report now: what is complete, which files were provably saved (exact paths), what remains unfinished, and an estimate of what is left. If nothing was provably saved, say so explicitly. Tool calls are disabled and will be rejected — write the report as plain prose, never as tool-call markup. The Lead will decide whether to continue the work in a linked continuation, accept the partial result, or stop. Do not claim unperformed tests, successful delivery, or completion without evidence. Do not ask for more budget or wait for another step. This instruction does not change the task or increase your allowance.`

export function closeoutForecast({ remainingTokens, remainingCalls, inputEstimate, finalInputEstimate }) {
  // Rail model: park when one more full step plus a viable report no longer
  // fit, then hand the continuation decision to the Lead. No output squeeze.
  const park = remainingTokens < inputEstimate + finalInputEstimate + CLOSEOUT_REPORT_FLOOR + CLOSEOUT_OUTPUT_RESERVE
  return {
    final_only: remainingCalls <= 1 || park,
    trigger: remainingCalls <= 1 ? 'last_available_call' : 'budget_rail',
    input_estimate: inputEstimate, final_input_estimate: finalInputEstimate,
    next_input_reserve: Math.max(inputEstimate, finalInputEstimate),
    report_floor: CLOSEOUT_REPORT_FLOOR, delivery_output_reserve: CLOSEOUT_OUTPUT_RESERVE,
    forecast_limitations: 'Estimated full input plus a report floor and a short delivery reserve; future tool-output growth and provider tokenization can still exceed this estimate. Hard admission remains authoritative.',
  }
}
