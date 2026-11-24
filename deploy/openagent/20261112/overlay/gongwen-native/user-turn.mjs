import { lstat, readFile } from 'node:fs/promises';
import { join } from 'node:path';

// The gateway owns both the session registry and its append-only message tree.
// Resolve only the transcript bound to this tool's host-assigned session identity.
export async function readUserTurn(ctx, callId, stateRoot = '/root/.openclaw/agents', toolName = 'gongwen_write') {
  const agent = /^agent:([a-z0-9_-]+):/.exec(ctx.sessionKey ?? '')?.[1];
  if (!agent || (ctx.agentId && ctx.agentId !== agent) || !/^[a-f0-9-]{36}$/i.test(ctx.sessionId) || !callId) return;
  try {
    const directory = join(stateRoot, agent, 'sessions');
    const registry = JSON.parse(await readFile(join(directory, 'sessions.json'), 'utf8'));
    const entry = registry[ctx.sessionKey];
    const file = join(directory, ctx.sessionId + '.jsonl');
    if (entry?.sessionId !== ctx.sessionId || (entry.sessionFile && entry.sessionFile !== file)) return;
    if (!(await lstat(file)).isFile()) return;
    const rows = (await readFile(file, 'utf8')).trimEnd().split('\n').map(line => JSON.parse(line));
    if (rows[0]?.type !== 'session' || rows[0].id !== ctx.sessionId) return;
    const nodes = new Map(rows.map(row => [row.id, row]));
    const currentCall = rows.findLast(row => row.message?.role === 'assistant' &&
      Array.isArray(row.message.content) && row.message.content.some(item =>
        item.type === 'toolCall' && item.name === toolName && item.id === callId));
    // Some gateway paths enter execute before appending the assistant tool-call node.
    let node = currentCall ?? (rows.at(-1)?.message?.role === 'user' ? rows.at(-1) : undefined);
    const seen = new Set();
    while (node && !seen.has(node.id)) {
      if (node.message?.role === 'user') {
        const { content, MediaPaths, MediaPath } = node.message;
        const prompt = typeof content === 'string' ? content : Array.isArray(content)
          ? content.filter(item => item.type === 'text').map(item => item.text).join('\n') : '';
        // WebChat persists upload metadata beside the message content on the exact user node.
        if (prompt.trim()) return { id: node.id, prompt,
          mediaPaths: MediaPaths === undefined ? (MediaPath === undefined ? [] : [MediaPath]) : MediaPaths,
          ...(toolName === 'gongwen_public_search'
          ? { alsoWriting: Boolean(currentCall?.message.content.some(item => item.type === 'toolCall' && item.name === 'gongwen_write')) }
          : {}) };
        return;
      }
      seen.add(node.id);
      node = nodes.get(node.parentId);
    }
  } catch {
    // Never substitute model-generated arguments for an unavailable user message.
  }
}
