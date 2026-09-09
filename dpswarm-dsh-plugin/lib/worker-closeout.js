// A forecast inside the existing worker grant, never an additional allocation.
export const CLOSEOUT_MARKER = 'DPSWARM_WORKER_FINAL_ONLY'
export const CLOSEOUT_OUTPUT_RESERVE = 2048
// Below this squeezed output size the next step cannot even write a full report
// or a meaningful file edit; close out while the final call still has room.
export const CLOSEOUT_REPORT_FLOOR = 4096
export const CLOSEOUT_INSTRUCTION = `[${CLOSEOUT_MARKER}]
This worker is now in final-only budget closeout. No tools are available. Stop expanding the task, inspecting files, repeating checks, or planning another request. Return your final delivery to the Lead now: identify files already saved successfully using evidence already present, explain what is complete, and list unfinished work or uncertainty. If a file was saved but no report was sent yet, report that saved candidate now. If nothing was saved, say so explicitly. Do not claim unperformed tests, successful delivery, or completion without evidence. Do not emit tool calls, ask for more budget, or wait for another step. This instruction does not change the task or increase your allowance.`

export function closeoutForecast({ remainingTokens, remainingCalls, inputEstimate, finalInputEstimate }) {
  const nextInput = Math.max(inputEstimate, finalInputEstimate)
  const unaffordable = remainingTokens < inputEstimate + nextInput + 2 * CLOSEOUT_OUTPUT_RESERVE
  // The output squeeze (outputLimit) would give the next exploration step this
  // much room. Below the report floor it truncates mid-report (17:32 run:
  // tester/reviewer died undelivered at 5,710/6,266 output). Close out while
  // the final delivery call — exempt from the squeeze — still has full room.
  const explorationOutput = Math.floor((remainingTokens - inputEstimate - nextInput - CLOSEOUT_OUTPUT_RESERVE) / 2)
  const belowFloor = explorationOutput < CLOSEOUT_REPORT_FLOOR
  return {
    final_only: remainingCalls <= 1 || unaffordable || belowFloor,
    trigger: remainingCalls <= 1 ? 'last_available_call'
      : unaffordable ? 'cannot_afford_exploration_and_delivery' : 'output_below_report_floor',
    input_estimate: inputEstimate, final_input_estimate: finalInputEstimate,
    next_input_reserve: nextInput, delivery_output_reserve: CLOSEOUT_OUTPUT_RESERVE,
    exploration_output_estimate: explorationOutput, report_floor: CLOSEOUT_REPORT_FLOOR,
    forecast_limitations: 'Estimated full input plus a short delivery reserve; future tool-output growth and provider tokenization can still exceed this estimate. Hard admission remains authoritative.',
  }
}
