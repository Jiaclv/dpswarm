export const CONTINUED = 'dpswarm/team-required-continued'
const BOUND = 'dpswarm/team-required-bound'
const ownsLifecycle = new Set(['dpswarm/team-required-started', 'dpswarm/team-required-finished', 'dpswarm/team-required-amended'])
const fail = message => Object.assign(new Error('TASK_CONTINUITY_INVALID: ' + message), { code: 'TASK_CONTINUITY_INVALID' })
const text = value => typeof value === 'string' && value.trim().length > 0
export const sameTaskBinding = (a, b) => ['root_session_id', 'binding_id', 'user_message_id', 'user_content_sha256'].every(k => typeof a?.[k] === 'string' && a[k] === b?.[k])

export function validateContinuationRequest(args) {
  if (!args || Object.keys(args).some(k => !['current_binding_id', 'previous_binding_id', 'reason'].includes(k))
    || !text(args.current_binding_id) || !text(args.previous_binding_id) || !text(args.reason)
    || args.reason.length > 2000 || args.current_binding_id === args.previous_binding_id) {
    throw Object.assign(new Error('TASK_CONTINUITY_REQUEST_INVALID: Identify the current and immediately preceding user bindings and explain why this is the same task.'), { code: 'TASK_CONTINUITY_REQUEST_INVALID' })
  }
}

/** Resolve only authenticated journal links; preserve the original execution binding. */
export function resolveTaskContinuation(snapshot, binding, seen = new Set()) {
  if (seen.has(binding.binding_id)) throw fail('Task continuation cycle.')
  seen.add(binding.binding_id)
  const events = (snapshot.events || []).filter(e => e.data?.root_session_id === binding.root_session_id)
  const bound = events.filter(e => e.type === BOUND && e.data.binding_id === binding.binding_id)
  if (bound.length !== 1 || !sameTaskBinding(bound[0].data, binding)) throw fail('The direct-user binding is missing or ambiguous.')
  const links = events.filter(e => e.type === CONTINUED && e.data.binding_id === binding.binding_id)
  if (!links.length) return { binding: bound[0].data, message_binding: binding, continuation: null }
  const link = links[0]?.data
  if (links.length !== 1 || !sameTaskBinding(link, binding) || link.owner_session_id !== binding.root_session_id
    || link.decision !== 'continue_same_task' || link.source_kind !== 'direct-user'
    || !text(link.reason) || link.reason.length > 2000 || !text(link.contract_id)
    || !Number.isSafeInteger(link.contract_revision) || link.contract_revision < 1
    || !text(link.run_id) || !text(link.previous_binding_id) || !text(link.effective_binding_id)
    || events.some(e => ownsLifecycle.has(e.type) && e.data.binding_id === binding.binding_id)) throw fail('A continuation cannot own or replace a lifecycle.')
  const previous = events.filter(e => e.type === BOUND && e.data.binding_id === link.previous_binding_id)
  if (previous.length !== 1 || events.indexOf(previous[0]) >= events.indexOf(bound[0])
    || events.indexOf(links[0]) <= events.indexOf(bound[0])) throw fail('The predecessor must already belong to this root.')
  const resolved = resolveTaskContinuation(snapshot, previous[0].data, seen)
  if (resolved.binding.binding_id !== link.effective_binding_id) throw fail('The recorded execution binding changed.')
  return { binding: resolved.binding, message_binding: binding, continuation: link }
}
