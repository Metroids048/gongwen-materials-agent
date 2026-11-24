import { test } from 'node:test';
import assert from 'node:assert/strict';
import { AsyncLocalStorage } from 'node:async_hooks';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { currentCommandEnvironment, loadRuntimeScope } from './runtime-environment.mjs';

const granted = run => ({ OASN_RUN_ID: run, OASN_CONSUMPTION_CONTEXT_REF: 'test-context-' + run,
  OPENAGENT_MODEL_API_KEY: 'unit-test-only', OPENAGENT_CHAT_BASE_URL: 'http://127.0.0.1/test-' + run,
  OPENAGENT_CHAT_MODEL: 'gpt-test', OPENAGENT_CHAT_INTERFACE_TYPE: 'openai-completions' });

test('current host scope replaces stale gateway route and removes developer billing context', () => {
  const current = granted('current');
  const inherited = { ...granted('old'), PATH: '/test/bin', OASN_DEVELOPER_WORKSPACE_ID: 'old-workspace', OASN_PAID_API_MCP_TOKEN: 'test-only' };
  const result = currentCommandEnvironment({ ownedRuntimeContext: () => ({ environment: current }), hasBoundResourceInvocation: () => true }, inherited);
  assert.equal(result.source, 'managed-invocation');
  for (const key of Object.keys(current)) assert.equal(result.environment[key], current[key]);
  assert.equal(result.environment.PATH, '/test/bin');
  assert.equal(result.environment.OASN_DEVELOPER_WORKSPACE_ID, undefined);
  assert.equal(result.environment.OASN_PAID_API_MCP_TOKEN, undefined);
  assert.equal(inherited.OASN_RUN_ID, 'old');
});

test('missing or incomplete formal scope never falls back to gateway environment', () => {
  assert.throws(() => currentCommandEnvironment({ ownedRuntimeContext: () => undefined, hasBoundResourceInvocation: () => true }, granted('old')), {code: 'RUNTIME_CONTEXT_UNAVAILABLE'});
  assert.throws(() => currentCommandEnvironment({ ownedRuntimeContext: () => ({environment: {OASN_RUN_ID: 'current'}}), hasBoundResourceInvocation: () => true }, granted('old')), {code: 'RUNTIME_CONTEXT_UNAVAILABLE'});
});

test('parallel host scopes remain isolated across await and subsequent invocations', async () => {
  const storage = new AsyncLocalStorage();
  const scope = { ownedRuntimeContext: () => storage.getStore(), hasBoundResourceInvocation: () => true };
  const results = await Promise.all(['one', 'two'].map(run => storage.run({environment: granted(run)}, async () => {
    await Promise.resolve();
    return currentCommandEnvironment(scope, {}).environment.OASN_RUN_ID;
  })));
  assert.deepEqual(results, ['one', 'two']);
  assert.throws(() => currentCommandEnvironment(scope, granted('old')), {code: 'RUNTIME_CONTEXT_UNAVAILABLE'});
});

test('developer fallback requires an actual workspace and no formal binding', () => {
  const scope = {ownedRuntimeContext: () => undefined, hasBoundResourceInvocation: () => false};
  assert.throws(() => currentCommandEnvironment(scope, granted('old')), {code: 'RUNTIME_CONTEXT_UNAVAILABLE'});
  assert.equal(currentCommandEnvironment(scope, {OASN_DEVELOPER_WORKSPACE_ID: 'test-workspace'}).source, 'developer-workspace');
});

test('SDK dependency loader shares the host scope and rejects an incompatible compiled SDK', async () => {
  const root = await mkdtemp(join(tmpdir(), 'gongwen-runtime-test-'));
  try {
    await mkdir(join(root, 'dist', 'plugin-sdk'), {recursive: true});
    await writeFile(join(root, 'package.json'), JSON.stringify({name: 'openclaw', type: 'module', exports: {'./plugin-sdk/oasn-invocation': './dist/plugin-sdk/oasn-invocation.js'}}));
    const ownerPath = join(root, 'dist', 'oasn-task-owner-fixture.js');
    await writeFile(ownerPath, `import {AsyncLocalStorage} from 'node:async_hooks';
const store = new AsyncLocalStorage();
function ownedRuntimeContext() {return store.getStore()}
export {ownedRuntimeContext as c};
export function inScope(value, fn) {return store.run(value, fn)}
`);
    const sdkPath = join(root, 'dist', 'plugin-sdk', 'oasn-invocation.js');
    await writeFile(sdkPath, `import {c} from '../oasn-task-owner-fixture.js';
export function hasBoundResourceInvocation() {return !!c()}
`);
    const host = await import(pathToFileURL(ownerPath).href);
    await host.inScope({environment: granted('real-scope')}, async () => {
      const loaded = await loadRuntimeScope(join(root, 'dist', 'entry.js'));
      assert.equal(currentCommandEnvironment(loaded, {}).environment.OASN_RUN_ID, 'real-scope');
    });
    await writeFile(sdkPath, 'export const hasBoundResourceInvocation = () => false;');
    await assert.rejects(loadRuntimeScope(join(root, 'dist', 'entry.js')), {code: 'RUNTIME_CONTEXT_UNAVAILABLE'});
  } finally {
    await rm(root, {recursive: true, force: true});
  }
});
