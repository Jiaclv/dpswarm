import { requireRootCaller } from './delegation.js'

const failure = (code, message) => Object.assign(new Error(`${code}: ${message}`), { code })
const text = value => typeof value === 'string' && value.length > 0

/** Owns one dispatch lifecycle; only the owner of a published child may settle it. */
export class TeamDispatcher {
  constructor({ controller, requirement }) {
    this.controller = controller
    this.requirement = requirement
    this.running = new Set()
  }

  #acquire(exec) {
    requireRootCaller(exec?.agent)
    const rootId = exec.agent.session.id
    if (this.running.has(rootId)) throw failure('RUN_PENDING', 'This root session already has a team operation in progress.')
    // Acquire synchronously before any journal read, including Code Mode calls.
    this.running.add(rootId)
    return rootId
  }

  async run(args, exec) {
    const rootId = this.#acquire(exec)
    try {
      const binding = await this.requirement.beforeDispatch(exec.agent)
      let published = false, runId
      const result = await this.controller.run(args, exec, {
        onChildStarted: async details => {
          await this.requirement.markStarted(binding, details)
          published = true
          runId = details.run_id
        },
      })
      // A native handle is not proof of a model request. On thrown errors or
      // uncertain physical cleanup, keep the durable requirement unsettled.
      const safelySettled = result && Array.isArray(result.failed) && result.failed.every(item =>
        // This trusted controller stage precedes child publication, so that
        // role has no physical worker or control-plane item to settle.
        item?.admission_stage === 'model_preflight'
        || (item?.control_settlement?.ok === true && item?.details?.physicalCleanupConfirmed !== false))
      if (published && safelySettled) {
        await this.requirement.finishRun(binding, {
          outcome: result.failed.length || result.stopped ? 'failed_takeover' : 'completed',
          run_id: runId,
        })
      }
      return result
    } finally {
      this.running.delete(rootId)
    }
  }

  async review(args, exec) {
    const rootId = this.#acquire(exec)
    try {
      const state = this.controller.session(exec.agent)
      // The controller marks a recovered lease only after observing its owner
      // PID has exited. Live/unknown workers cannot be released by review alone.
      const lease = state.lease
      const recovered = lease?.recovered === true && state.cfg?.subagentProvider === 'spawn'
        && lease.session_id === rootId && Number.isSafeInteger(lease.pid) && lease.pid > 0 && text(lease.run_id)
        ? { run_id: lease.run_id, pid: lease.pid } : null
      const result = await this.controller.review(args, exec)
      if (!recovered || state.lease !== null || state.busy !== false) return result
      const binding = await this.requirement.beforeRun(exec.agent)
      if (!binding.required || binding.phase !== 'started') return result
      const starts = binding.starts.map(event => event.data)
      if (!starts.length || starts.some(start => start.run_id !== recovered.run_id || !text(start.execution_session_id))) return result
      const status = await this.controller.status(exec.agent)
      const snapshot = status?.snapshot
      if (!snapshot || snapshot.open_worker_slots_used !== 0 || !snapshot.nodes || !snapshot.work_items) return result
      const nodes = Object.values(snapshot.nodes)
      const terminal = starts.every(start => {
        const matches = nodes.filter(node => node.execution_session_id === start.execution_session_id)
        if (matches.length !== 1) return false
        const node = matches[0]
        return node.execution_parent_session_id === rootId && node.execution_provider === 'spawn'
          && node.lifecycle === 'drained' && text(node.item)
          && ['accepted', 'terminated'].includes(snapshot.work_items[node.item]?.acceptance)
      })
      if (terminal) {
        await this.requirement.finishRecovered(exec.agent, {
          run_id: recovered.run_id,
          execution_session_ids: starts.map(start => start.execution_session_id),
          control_plane_confirmed: true,
          all_workers_terminal: true,
          physical_cleanup_confirmed: true,
          open_worker_slots_used: 0,
        })
      }
      return result
    } finally {
      this.running.delete(rootId)
    }
  }
}
