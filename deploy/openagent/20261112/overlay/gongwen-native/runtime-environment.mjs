import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { pathToFileURL } from 'node:url';

const removedScopedKeys = [
  'OASN_PAID_API_MCP_TOKEN', 'OASN_BILLING_ROOT_NODE_ID',
  'OASN_DEVELOPER_WORKSPACE_ID', 'OASN_DEVELOPER_WORKSPACE_OUTPUT_URL',
  'OASN_DEVELOPER_WORKSPACE_IMPORT_URL', 'OASN_DEVELOPER_WORKSPACE_DISCARD_URL',
];

function unavailable() {
  return Object.assign(new Error('当前运行上下文不可用，未调用公文模型。'), { code: 'RUNTIME_CONTEXT_UNAVAILABLE' });
}

// shortcut: this platform SDK has no public environment accessor; fail closed if its verified dependency changes.
export async function loadRuntimeScope(entry = process.argv[1]) {
  let folder = dirname(entry);
  while (true) {
    try {
      const manifest = join(folder, 'package.json');
      if (JSON.parse(await readFile(manifest, 'utf8')).name === 'openclaw') {
        const resolveSdk = createRequire(manifest).resolve;
        const sdkPath = resolveSdk('openclaw/plugin-sdk/oasn-invocation');
        const source = await readFile(sdkPath, 'utf8');
        const dependency = source.match(/from\s+["'](\.\.\/oasn-task-owner-[\w-]+\.js)["']/)?.[1];
        if (!dependency) throw unavailable();
        const ownerPath = join(dirname(sdkPath), dependency);
        const ownerSource = await readFile(ownerPath, 'utf8');
        const ownedExport = ownerSource.match(/\bownedRuntimeContext\s+as\s+(\w+)/)?.[1];
        const owner = await import(pathToFileURL(ownerPath).href);
        const sdk = await import(pathToFileURL(sdkPath).href);
        if (typeof owner[ownedExport] !== 'function' || typeof sdk.hasBoundResourceInvocation !== 'function') throw unavailable();
        let emitDiagnosticEvent;
        try {
          const diagnostics = resolveSdk('openclaw/plugin-sdk/diagnostic-runtime');
          ({ emitDiagnosticEvent } = await import(pathToFileURL(diagnostics).href));
        } catch (error) {
          if (!['ERR_PACKAGE_PATH_NOT_EXPORTED', 'MODULE_NOT_FOUND'].includes(error.code)) throw error;
        }
        return { ownedRuntimeContext: owner[ownedExport], hasBoundResourceInvocation: sdk.hasBoundResourceInvocation,
          emitDiagnosticEvent };
      }
    } catch (error) {
      if (error.code !== 'ENOENT' && !(error instanceof SyntaxError)) throw unavailable();
    }
    const parent = dirname(folder);
    if (parent === folder) throw unavailable();
    folder = parent;
  }
}

export function currentCommandEnvironment(scope, inherited = process.env) {
  const current = scope.ownedRuntimeContext();
  if (!current) {
    if (scope.hasBoundResourceInvocation() || !inherited.OASN_DEVELOPER_WORKSPACE_ID) throw unavailable();
    return { environment: { ...inherited }, source: 'developer-workspace' };
  }
  const environment = { ...inherited };
  for (const key of removedScopedKeys) delete environment[key];
  Object.assign(environment, current.environment);
  for (const key of ['OASN_RUN_ID', 'OASN_CONSUMPTION_CONTEXT_REF', 'OPENAGENT_MODEL_API_KEY',
    'OPENAGENT_CHAT_BASE_URL', 'OPENAGENT_CHAT_MODEL', 'OPENAGENT_CHAT_INTERFACE_TYPE']) {
    if (typeof current.environment?.[key] !== 'string' || !current.environment[key]) throw unavailable();
  }
  return { environment, source: 'managed-invocation' };
}
