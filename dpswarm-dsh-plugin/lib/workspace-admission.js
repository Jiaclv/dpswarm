import { createHash } from 'node:crypto'
import { readFileSync, realpathSync } from 'node:fs'
import { join } from 'node:path'
import { runtimePaths } from './paths.js'

const hash = value => createHash('sha256').update(value).digest('hex')
const processState = pid => {
  if (!Number.isSafeInteger(pid) || pid <= 0) return null
  try { process.kill(pid, 0); return true } catch (error) { return error.code === 'ESRCH' ? false : null }
}

/** Read only. The controller still exclusively owns atomic acquisition/recovery. */
export function inspectWorkspaceAdmission(config, agent, { processStatus = processState } = {}) {
  const cwd = agent?.session?.header?.cwd
  if (typeof cwd !== 'string' || !cwd) return null
  const actual = realpathSync(cwd), identity = process.platform === 'win32' ? actual.toLowerCase() : actual
  const { workspace } = runtimePaths(config)
  const path = join(workspace, 'workspace-leases', hash(JSON.stringify(identity)) + '.json')
  let raw
  try { raw = readFileSync(path, 'utf8') } catch (error) {
    if (error.code === 'ENOENT') return null
    return { code: 'WORKSPACE_BUSY', fingerprint: hash(JSON.stringify([path, error.code])),
      lease_path: path, owner_session_id: null, pid: null, pid_alive: null,
      message: 'The workspace lease cannot be read. Verify its owner and cleanup before retrying; it was not removed.' }
  }
  let lease
  try { lease = JSON.parse(raw) } catch { /* malformed ownership is not evidence of cleanup */ }
  const alive = processStatus(lease?.pid)
  // The owning root must retain access to review/terminate its own deliveries.
  if (lease?.session_id === agent.session.id || alive === false) return null
  return { code: 'WORKSPACE_BUSY', fingerprint: hash(JSON.stringify([path, raw, alive])), lease_path: path,
    owner_session_id: typeof lease?.session_id === 'string' ? lease.session_id : null,
    pid: Number.isSafeInteger(lease?.pid) ? lease.pid : null, pid_alive: alive,
    message: 'Another session or unverified owner holds this workspace lease. Review or terminate the previous run in its owning session, then retry. No team was started and no workspace lock was removed.' }
}
