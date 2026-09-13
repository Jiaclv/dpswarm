// Advisory classification of worker final-output text. Tools are already
// absent from closeout requests, so tool-call markup in the text never
// executed. The detector only makes that fact visible to the Lead; it never
// blocks, rewrites, or executes anything.
//
// Observed families:
// 1. DSML markup (pelican-014 live run, all three roles):
//      <｜｜DSML｜｜ calls>
//      <｜｜DSML｜｜ invoke name="pwsh">
//      <｜｜DSML｜｜ parameter name="command" string="true">…</｜｜DSML｜｜ parameter>
//      </｜｜DSML｜｜ invoke>
//      </｜｜DSML｜｜ calls>
// 2. Provider special tokens leaking as text:
//      <｜tool▁calls▁begin｜><｜tool▁call▁begin｜>function<｜tool▁sep｜>…

const DSML_TAG = /<\/?[|｜]+DSML[|｜]+[^>\n]*>/g
const PROVIDER_TOKEN = /<\/?｜tool▁[^>\n]*>/g
const INVOKE_NAME = /\binvoke\s+name="([^"<]{1,100})"/g

export const PSEUDO_MARKUP_GUIDANCE = 'The final text contains tool-call markup (DSML or provider tokens) that was never executed: any checks or edits it describes did not run, and a structured report may be missing or invalid. Judge from actually executed evidence; for a tester/reviewer report use dpswarm_repair_report.'

export function pseudoToolCallMarkup(source) {
  const text = typeof source === 'string' ? source : ''
  if (!text) return null
  const dsml = text.match(DSML_TAG) || []
  const provider = text.match(PROVIDER_TOKEN) || []
  if (!dsml.length && !provider.length) return null
  const families = []
  if (dsml.length) families.push('dsml-markup')
  if (provider.length) families.push('provider-special-tokens')
  const toolNames = []
  if (dsml.length) {
    for (const match of text.matchAll(INVOKE_NAME)) {
      if (!toolNames.includes(match[1])) toolNames.push(match[1])
    }
  }
  return { families, tool_names: toolNames, marker_count: dsml.length + provider.length }
}
