import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { readUserTurn } from './user-turn.mjs';
import { registerDocumentTurns } from './index.mjs';

test('gateway retries resolve their own user ancestor even after another user sends a message', async t => {
  const root = await mkdtemp(join(tmpdir(), 'gongwen-source-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const sessionId = 'ea29d257-b372-493f-843c-a7e6ae87a1af';
  const sessionKey = 'agent:main:dashboard:one';
  const ctx = { agentId: 'main', sessionKey, sessionId };
  const directory = join(root, 'main', 'sessions');
  await mkdir(directory, { recursive: true });
  const file = join(directory, sessionId + '.jsonl');
  await writeFile(join(directory, 'sessions.json'), JSON.stringify({ [sessionKey]: { sessionId, sessionFile: file } }));
  const user = (id, parentId, content) => ({ type: 'message', id, parentId, message: { role: 'user', content } });
  const call = (id, parentId, callId) => ({ type: 'message', id, parentId, message: { role: 'assistant',
    content: [{ type: 'toolCall', name: 'gongwen_write', id: callId, arguments: { request: '擅自加入的数字' } }] } });
  const attachment = '/root/.openclaw/media/inbound/会商材料---upload.txt';
  const firstUser = user('first', null, '只要正文。');
  Object.assign(firstUser.message, { MediaPath: attachment, MediaPaths: [attachment], MediaType: 'text/plain' });
  const rows = [{ type: 'session', id: sessionId }, firstUser, call('c1', 'first', 'tool-1'),
    call('c2', 'c1', 'tool-2'), user('next', 'c2', [{ type: 'text', text: '这份需要 Word。' }])];
  await writeFile(file, rows.map(row => JSON.stringify(row)).join('\n') + '\n');
  const resolve = (host, id) => readUserTurn(host, id, root);
  assert.equal(await resolve({ ...ctx, sessionKey: 'agent:main:dashboard:other' }, 'tool-1'), undefined);
  assert.deepEqual((await resolve(ctx, 'tool-1')).mediaPaths, [attachment]);
  assert.deepEqual((await resolve(ctx, 'current-unwritten-call')).mediaPaths, []);
  const run = registerDocumentTurns({ on() {} }, resolve);
  const starts = [];
  const execute = async (text, id, files) => { starts.push(text); return { status: text, files }; };
  const [first, retry] = await Promise.all([run(ctx, 'tool-1', execute), run(ctx, 'tool-2', execute)]);
  assert.equal(first, retry);
  assert.deepEqual(first.files, [attachment]);
  assert.deepEqual(starts, ['只要正文。']);
  assert.deepEqual(await run(ctx, 'current-unwritten-call', execute), { status: '这份需要 Word。', files: [] });
  assert.deepEqual(starts, ['只要正文。', '这份需要 Word。']);
});

test('the public evidence tool waits for the user arrangement and looks up an expressly requested event only once', async t => {
  const { publicSearchTool } = await import('./public-search.mjs');
  const http = [];
  t.mock.method(globalThis, 'fetch', async (_, init) => {
    const request = JSON.parse(init.body);
    http.push(request.method);
    return new Response(JSON.stringify({ result: { content: [{ type: 'text', text: JSON.stringify({
      results: [{ title: '公开公告', url: 'https://www.apec.org/meeting', excerpts: ['11月18日开会'] }],
    }) }] } }), { headers: { 'Content-Type': 'application/json' } });
  });
  const ctx = { sessionKey: 'agent:main:case', sessionId: 'own' };
  const params = { event: 'APEC', city: '深圳' };
  const factory = (id, prompt, alsoWriting) => publicSearchTool(ctx, async () => ({ id, prompt, alsoWriting }));
  const before = await factory('first', '发给深圳的上班族，apec正式会议开始前一天和结束后一天，发文单位是深圳市政府。', true)
    .execute('search1', params);
  assert.equal(JSON.parse(before.content[0].text).status, 'not_needed');
  assert.equal(http.length, 0);
  const [first, retry] = await Promise.all(['call2', 'call3'].map(id =>
    factory('next', '请查一下深圳 APEC 正式会期，附官方链接。', false).execute(id, params)));
  assert.deepEqual(retry, first);
  assert.equal(JSON.parse(first.content[0].text).sources[0].url, 'https://www.apec.org/meeting');
  assert.deepEqual(http, ['initialize', 'notifications/initialized', 'tools/call']);
});
