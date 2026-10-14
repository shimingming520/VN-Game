import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const routerSource = fs.readFileSync(
  path.join(frontendRoot, 'src/router/index.js'),
  'utf8',
)
const requestSource = fs.readFileSync(
  path.join(frontendRoot, 'src/api/request.js'),
  'utf8',
)

test('anonymous users can enter the platform without a login redirect', () => {
  assert.doesNotMatch(routerSource, /next\(['"]\/login['"]\)/)
  assert.doesNotMatch(routerSource, /isLoggedIn\(/)
  assert.doesNotMatch(requestSource, /window\.location\.href\s*=\s*['"]\/login['"]/)
})
