#!/usr/bin/env node
import { spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, readdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve, delimiter } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { resolveHostRoot } from '../lib/host-modules.js'
import { defaultStateDirectory, runtimePaths } from '../lib/paths.js'

const plugin = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const help = `DPswarm DSH fixed-team installer
  node dpswarm-dsh-plugin/bin/setup.mjs --check
  node dpswarm-dsh-plugin/bin/setup.mjs --install [--python <executable>] [--profile web]
Options:
  --check              Read-only diagnostics (default); no model calls
  --install            Install Python into a private directory and register the DSH plugin
  --python <path>      Python 3.10+ with pip and setuptools 68+
  --source <path>      Python project directory (default: sibling dpswarm-plugin)
  --state-dir <path>   Runtime/state directory; host must use the same DPSWARM_STATE_DIR
  --dsh-home <path>    Isolated DSH home for installation/verification
  --profile <name>     Target DSH profile (default: web)
After installation, restart DSH, configure exact provider names and enable only the desired task.
`

function options(argv) {
  const out = { install: false, profile: 'web' }
  const values = new Set(['python', 'profile', 'source', 'state-dir', 'dsh-home'])
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i]
    if (flag === '--help' || flag === '-h') { console.log(help); process.exit(0) }
    if (flag === '--check') continue
    if (flag === '--install') { out.install = true; continue }
    const key = flag.slice(2)
    if (!flag.startsWith('--') || !values.has(key) || !argv[i + 1] || argv[i + 1].startsWith('--')) throw new Error(`Unknown or incomplete option: ${flag}`)
    out[key] = argv[++i]
  }
  if (!/^[A-Za-z0-9_-]+$/.test(out.profile)) throw new Error('Use a simple DSH profile name')
  return out
}

function execute(cmd, args, { env = process.env, cwd, inherit = false } = {}) {
  const result = spawnSync(cmd, args, { env, cwd, shell: false, windowsHide: true,
    encoding: 'utf8', stdio: inherit ? 'inherit' : 'pipe', timeout: 180000 })
  if (result.error || result.status !== 0) {
    throw new Error(result.error?.message || `${cmd} failed (${result.status}): ${(result.stderr || '').trim().slice(-1400)}`)
  }
  return result.stdout?.trim() || ''
}

async function main() {
  const opt = options(process.argv.slice(2))
  const env = { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' }
  // The DSH CLI forwards to pnpm; keep the current Node binary discoverable on Windows.
  const pathKey = Object.keys(env).find(key => key.toUpperCase() === 'PATH') || 'PATH'
  env[pathKey] = dirname(process.execPath) + delimiter + (env[pathKey] || '')
  if (opt['dsh-home']) env.DSH_HOME = resolve(opt['dsh-home'])
  if (opt['state-dir']) env.DPSWARM_STATE_DIR = resolve(opt['state-dir'])
  const state = defaultStateDirectory(env)
  const paths = runtimePaths({ workspace: state })
  const python = opt.python || env.DPSWARM_PYTHON || paths.pythonCmd
  const source = resolve(opt.source || join(plugin, '../dpswarm-plugin'))
  const hostRoot = resolveHostRoot({ env })
  const host = resolve(hostRoot, '../..')
  const manifest = JSON.parse(readFileSync(join(host, 'package.json'), 'utf8'))
  const [{ BasicCompactionEngine }, { Session }] = await Promise.all([
    import(pathToFileURL(join(hostRoot, 'dsh-compaction-basic/lib/index.js')).href),
    import(pathToFileURL(join(hostRoot, 'dsh-session/lib/index.js')).href)
  ])
  if (typeof BasicCompactionEngine?.prototype?.compactRegion !== 'function' || typeof Session?.prototype?.deriveMessages !== 'function') throw new Error('DSH public CM history API unavailable')
  console.log('DSH public CM transaction and history API: available')
  const cli = join(host, typeof manifest.bin === 'string' ? manifest.bin : manifest.bin.dsh)
  if (!existsSync(cli)) throw new Error('DSH command entry is missing')
  const runtime = JSON.parse(execute(python, ['-c', 'import sys,json; assert sys.version_info >= (3,10), "Python 3.10+ required"; print(json.dumps({"executable":sys.executable,"version":sys.version.split()[0]}))'], { env }))
  console.log(`DSH ${manifest.version}: ${host}`)
  console.log(`Python ${runtime.version}: ${runtime.executable}`)
  console.log(`Runtime directory: ${state}`)
  const packageDir = paths.dpswarmDir || source
  if (!opt.install) {
    execute(runtime.executable, ['-c', 'from pathlib import Path; from dpswarm.session_server import BRIDGE; assert BRIDGE["session_isolation"] is True; print("Fixed-team sidecar import OK")'], { env, cwd: existsSync(packageDir) ? packageDir : undefined })
    console.log(`Fixed-team sidecar: import OK (${packageDir})`)
    console.log(existsSync(join(state, 'installation.json')) ? 'Private installation record: present' : 'Source checkout import only; run --install to register and package the plugin.')
    console.log('Manual activation: off by default. Provider credentials, model calls and live UI are not probed by --check.')
    return
  }
  if (!existsSync(join(source, 'pyproject.toml')) || !existsSync(join(source, 'dpswarm/session_server.py'))) throw new Error('Python source not found; clone the full repository or pass --source')
  execute(runtime.executable, ['-c', 'import pip,importlib.metadata; assert int(importlib.metadata.version("setuptools").split(".")[0]) >= 68, "setuptools 68+ required"'], { env })
  const leases = join(state, 'workspace-leases')
  if (existsSync(leases) && readdirSync(leases).some(name => name.endsWith('.json'))) throw new Error('This runtime has unfinished workspace leases. Finish review/cleanup before updating, or use an independent state directory.')
  const target = join(state, 'python')
  mkdirSync(state, { recursive: true })
  execute(runtime.executable, ['-m', 'pip', 'install', '--disable-pip-version-check', '--no-index', '--no-deps', '--no-build-isolation', '--upgrade', '--target', target, source], { env, inherit: true })
  execute(runtime.executable, ['-c', 'from dpswarm.session_server import BRIDGE; assert BRIDGE["session_isolation"]'], { env, cwd: target })
  const packed = JSON.parse(execute(runtime.executable, [join(plugin, 'bin/pack_plugin.py'), plugin, join(state, 'artifacts')], { env }))
  const spec = `file:${packed.archive.replaceAll('\\', '/')}`
  // DSH 0.1.x forwards through cmd.exe on Windows: quote spaces and reject expansion characters.
  if (process.platform === 'win32' && /[\"%!\r\n]/.test(spec)) throw new Error('DSH Windows forwarding does not support quotes, percent or exclamation marks in the plugin path')
  const packageArg = process.platform === 'win32' ? `\"${spec}\"` : spec
  execute(process.execPath, [cli, 'plugin', '--profile', opt.profile, 'add', packageArg, '--ignore-scripts'], { env, inherit: true })
  const record = { version: 1, sourceSha256: packed.source_sha256, pythonCmd: runtime.executable, dpswarmDir: target, pluginVersion: JSON.parse(readFileSync(join(plugin, 'package.json'), 'utf8')).version }
  const pending = join(state, `installation.${process.pid}.tmp`)
  writeFileSync(pending, JSON.stringify(record, null, 2) + '\n', { flag: 'wx' })
  renameSync(pending, join(state, 'installation.json'))
  console.log(`Installed into DSH profile ${opt.profile}. Restart that profile, open Settings > DPswarm to save role models, then enable the desired task switches.`)
  console.log('Independent CM defaults to DeepSeek and is configurable in Settings > DPswarm, off by default. No task was enabled and no model was called.')
  if (opt['state-dir']) console.log(`Start DSH with DPSWARM_STATE_DIR=${state} (or set its runtime directory to this path).`)
}
main().catch(error => { console.error(`DPswarm setup: ${error.message}`); process.exitCode = 1 })
