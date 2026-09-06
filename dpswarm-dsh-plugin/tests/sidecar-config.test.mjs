import assert from 'node:assert/strict'
import test from 'node:test'
import { Sidecar, sidecarSpawnSpec } from '../lib/sidecar.js'

test('sidecar launch honors port, exact executable and hidden Windows process options', () => {
  const config = { sidecarUrl: 'http://127.0.0.1:19876', pythonCmd: 'C:/Python/python.exe', dpswarmDir: 'C:/workspace/plugin' }
  const launch = sidecarSpawnSpec(config)
  assert.equal(launch.command, config.pythonCmd)
  assert.deepEqual(launch.args, ['-m', 'dpswarm.server', '--port', '19876'])
  assert.equal(launch.options.cwd, config.dpswarmDir)
  assert.equal(launch.options.windowsHide, true)
  assert.equal(launch.options.shell, false)
})

test('sidecar connection normalizes an origin and rejects unsupported remote or credential-bearing URLs', () => {
  assert.equal(new Sidecar({ sidecarUrl: 'http://127.0.0.1:19876/' }).cfg.sidecarUrl, 'http://127.0.0.1:19876')
  for (const sidecarUrl of ['https://example.com', 'http://user:secret@127.0.0.1:8791', 'http://127.0.0.1:8791/path']) {
    assert.throws(() => new Sidecar({ sidecarUrl }), /local HTTP origin/)
  }
})
