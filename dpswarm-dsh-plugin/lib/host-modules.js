import { existsSync, realpathSync } from 'node:fs'
import { createRequire, globalPaths } from 'node:module'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const valid = p => existsSync(join(p, 'dsh-tools/lib/index.js')) && existsSync(join(p, 'schemastery/lib/index.cjs'))

/** Resolve the running host's module copies; never install a second Cordis tree. */
export function resolveHostRoot({ env = process.env, argv = process.argv, execPath = process.execPath } = {}) {
  if (env.DSH_HOST_ROOT) {
    const path = resolve(env.DSH_HOST_ROOT)
    if (!valid(path)) throw new Error('DSH_HOST_ROOT does not contain the DSH host modules')
    return realpathSync(path)
  }
  const roots = [dirname(fileURLToPath(import.meta.url)), argv[1] && dirname(resolve(argv[1]))].filter(Boolean)
  const candidates = []
  for (const start of roots) {
    let dir = start
    while (true) {
      candidates.push(join(dir, 'node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai'), join(dir, 'node_modules/@deepseek-ai'))
      if (dirname(dir) === dir) break
      dir = dirname(dir)
    }
    try {
      const pkg = createRequire(join(start, '__dpswarm_resolver.cjs')).resolve('@deepseek-ai/dsh/package.json')
      candidates.unshift(join(dirname(pkg), 'node_modules/@deepseek-ai'))
    } catch { /* The host package can restrict package.json through exports. */ }
  }
  for (const base of globalPaths) candidates.push(join(base, '@deepseek-ai/dsh/node_modules/@deepseek-ai'))
  if (env.APPDATA) candidates.push(join(env.APPDATA, 'npm/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai'))
  candidates.push(resolve(dirname(execPath), '../lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai'))
  const match = candidates.find(valid)
  if (!match) throw new Error('DSH host not found. Install @deepseek-ai/dsh or set DSH_HOST_ROOT to its node_modules/@deepseek-ai directory.')
  return realpathSync(match)
}

export const hostModuleUrl = (root, relative) => pathToFileURL(join(root, relative)).href
