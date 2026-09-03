/** Consume the public DSH one-shot SubagentRun lifecycle.
 *
 * start() publishes a child handle; only handle.result is terminal evidence.
 * Every published handle is disposed, including cancellation and failed runs.
 * Nothing in this module executes or submits a tool on the child's behalf.
 */
export class SubagentRunError extends Error {
  constructor(code, message, details = {}) {
    super(message)
    this.name = 'SubagentRunError'
    this.code = code
    this.details = details
  }
}

function aborted(signal) {
  if (signal?.aborted) {
    throw new SubagentRunError('SUBAGENT_ABORTED', 'Delegation was cancelled')
  }
}

async function waitResult(result, signal) {
  aborted(signal)
  if (!signal) return await result
  let onAbort
  const cancellation = new Promise((_, reject) => {
    onAbort = () => reject(new SubagentRunError('SUBAGENT_ABORTED', 'Delegation was cancelled'))
    signal.addEventListener('abort', onAbort, { once: true })
    // Close the race between the earlier check and listener registration.
    if (signal.aborted) onAbort()
  })
  try {
    return await Promise.race([result, cancellation])
  } finally {
    signal.removeEventListener('abort', onAbort)
  }
}

function outputOf(result, sessionId) {
  if (!result || typeof result !== 'object' || typeof result.stopReason !== 'string'
      || !result.stopReason || !Array.isArray(result.output)) {
    throw new SubagentRunError('SUBAGENT_INVALID_RESULT', 'DSH returned no valid terminal result', { sessionId })
  }
  const text = result.output.filter(block => block?.type === 'text' && typeof block.text === 'string')
    .map(block => block.text).join('\n')
  const details = { sessionId, stopReason: result.stopReason, output: text,
    ...(typeof result.diagnostic === 'string' ? { diagnostic: result.diagnostic } : {}) }
  if (result.stopReason !== 'completed') {
    throw new SubagentRunError('SUBAGENT_NOT_COMPLETED', `DSH child ended with ${result.stopReason}`, details)
  }
  if (!text.trim()) {
    throw new SubagentRunError('SUBAGENT_EMPTY_DELIVERY', 'Completed DSH child has no textual delivery', details)
  }
  return { text, stopReason: result.stopReason, sessionId }
}

/** Await terminal evidence and quiescent disposal before returning a delivery.
 * onPublished can bind the trusted handle identity to a control-plane lease.
 * It is not model supplied; a binding failure also disposes the child.
 */
export async function runSubagentToCompletion(subagents, provider, request, { onPublished } = {}) {
  aborted(request.signal)
  const run = await subagents.start(provider, request)
  let failure, delivery
  try {
    if (!run || typeof run.id !== 'string' || !run.id || typeof run.dispose !== 'function'
        || !run.result || typeof run.result.then !== 'function') {
      throw new SubagentRunError('SUBAGENT_INVALID_HANDLE', 'DSH start() did not return a disposable run handle')
    }
    // Attach immediately so an infrastructure rejection is observed even if
    // the publication/binding callback fails before waiting for the result.
    const result = Promise.resolve(run.result)
    result.catch(() => {})
    aborted(request.signal)
    if (onPublished) await onPublished(run)
    delivery = outputOf(await waitResult(result, request.signal), run.id)
    aborted(request.signal)
  } catch (error) {
    failure = error
  } finally {
    if (typeof run?.dispose === 'function') {
      try {
        await run.dispose()
      } catch (error) {
        failure = new SubagentRunError('SUBAGENT_DISPOSAL_FAILED',
          'Child quiescence could not be established; delivery cannot be submitted',
          { sessionId: run.id, cause: String(error?.message ?? error),
            ...(failure ? { originalError: String(failure?.message ?? failure) } : {}) })
      }
    }
  }
  if (failure) throw failure
  aborted(request.signal)
  return delivery
}
