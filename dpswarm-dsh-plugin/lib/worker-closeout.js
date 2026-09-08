// A forecast inside the existing worker grant, never an additional allocation.
export const CLOSEOUT_MARKER = 'DPSWARM_WORKER_FINAL_ONLY'
export const CLOSEOUT_OUTPUT_RESERVE = 2048
export const CLOSEOUT_INSTRUCTION = `[${CLOSEOUT_MARKER}]
This worker is now in final-only budget closeout. No tools are available. Stop expanding the task, inspecting files, repeating checks, or planning another request. Return your final delivery to the Lead now: identify files already saved successfully using evidence already present, explain what is complete, and list unfinished work or uncertainty. If a file was saved but no report was sent yet, report that saved candidate now. If nothing was saved, say so explicitly. Do not claim unperformed tests, successful delivery, or completion without evidence. Do not emit tool calls, ask for more budget, or wait for another step. This instruction does not change the task or increase your allowance.`

export function closeoutForecast({ remainingTokens, remainingCalls, inputEstimate, finalInputEstimate }) {
  const nextInput = Math.max(inputEstimate, finalInputEstimate)
  return {
    final_only: remainingCalls <= 1 || remainingTokens < inputEstimate + nextInput + 2 * CLOSEOUT_OUTPUT_RESERVE,
    trigger: remainingCalls <= 1 ? 'last_available_call' : 'cannot_afford_exploration_and_delivery',
    input_estimate: inputEstimate, final_input_estimate: finalInputEstimate,
    next_input_reserve: nextInput, delivery_output_reserve: CLOSEOUT_OUTPUT_RESERVE,
    forecast_limitations: 'Estimated full input plus a short delivery reserve; future tool-output growth and provider tokenization can still exceed this estimate. Hard admission remains authoritative.',
  }
}
