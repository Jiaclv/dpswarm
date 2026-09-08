import { existsSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

export function defaultStateDirectory(env = process.env) {
  const base = env.LOCALAPPDATA || env.XDG_STATE_HOME || join(homedir(), '.local/state')
  return env.DPSWARM_STATE_DIR ? resolve(env.DPSWARM_STATE_DIR) : join(base, 'dpswarm', 'dsh')
}

export function runtimePaths(cfg = {}) {
  const workspace = cfg.workspace ? resolve(cfg.workspace) : defaultStateDirectory()
  const marker = join(workspace, 'installation.json')
  let installed = {}
  if (existsSync(marker)) {
    installed = JSON.parse(readFileSync(marker, 'utf8'))
    if (installed.version !== 1 || typeof installed.pythonCmd !== 'string' || typeof installed.dpswarmDir !== 'string') {
      throw new Error('Invalid DPSwarm installation record; rerun the installer')
    }
  }
  const sibling = resolve(dirname(fileURLToPath(import.meta.url)), '../../dpswarm-plugin')
  const dpswarmDir = cfg.dpswarmDir ? resolve(cfg.dpswarmDir) : installed.dpswarmDir ||
    (existsSync(join(sibling, 'dpswarm/server.py')) ? sibling : undefined)
  return { dpswarmDir, workspace, pythonCmd: cfg.pythonCmd || process.env.DPSWARM_PYTHON || installed.pythonCmd || 'python' }
}
