import { test } from 'node:test';
import assert from 'node:assert/strict';
import plugin, { nativeDeliveryResult, registerDocumentTurns, trustedEnvelope } from './index.mjs';

test('gateway tool factories share the first authentic user input and reply only within the host turn', async () => {
  const hooks = new Map();
  const once = registerDocumentTurns({ on: (name, handler) => hooks.set(name, handler) });
  const host = { sessionKey: 'owner', sessionId: 'session-one', runId: 'run-one' };
  const original = '两天分别安排，各一天。成文日期留空。';
  hooks.get('agent_turn_prepare')({ prompt: original }, host);
  const bind = (ctx, toolCallId) => hooks.get('before_tool_call')(
    { toolName: 'gongwen_write', runId: ctx.runId, toolCallId }, ctx);
  bind(host, 'first');
  bind(host, 'model-retry');
  let starts = 0;
  const first = once(host, 'first', async raw => { starts++; assert.equal(raw, original); return { result: '待补问' }; });
  hooks.get('agent_turn_prepare')({ prompt: '模型重新解释的用户问题' }, host);
  const retry = once(host, 'model-retry', () => { throw Error('changed original'); });
  assert.equal(await first, await retry);
  assert.equal(starts, 1);
  assert.equal(await once({ ...host, sessionId: 'other' }, 'first', async raw => raw), undefined);
  const next = { ...host, runId: 'run-two' };
  hooks.get('agent_turn_prepare')({ prompt: '用户刚确认的新事实' }, next);
  bind(next, 'new-user-call');
  assert.equal(await once(next, 'new-user-call', async raw => raw), '用户刚确认的新事实');
});

test('a recreated native tool returns the identical first result for a rewritten request', async () => {
  let factory;
  const hooks = new Map();
  plugin.register({ on: (name, handler) => hooks.set(name, handler), registerTool(value, options) {
    if (options.name === 'gongwen_write') factory = value;
  } });
  const host = { sessionId: 'gateway-session', sessionKey: 'gateway-key', runId: 'gateway-run' };
  hooks.get('agent_turn_prepare')({ prompt: '日期保留相对表述。' }, host);
  for (const toolCallId of ['call-original', 'call-rewritten']) {
    hooks.get('before_tool_call')({ toolName: 'gongwen_write', runId: host.runId, toolCallId }, host);
  }
  const original = factory(host).execute('call-original', { request: '日期保留相对表述。' });
  const repeated = factory(host).execute('call-rewritten', { request: '用户授权了新日期。' });
  assert.equal(original, repeated);
  assert.equal(await original, await repeated);
});

test('identity comes from host context even if model supplies identity-looking arguments', () => {
  const result = trustedEnvelope({ sessionId: 'host-session', sessionKey: 'host-key', requesterSenderId: 'host-owner' }, 'host-call',
    { request: '写通知', session_id: 'other-session', consumer_id: 'other-owner' });
  assert.equal(result.identity.session_id, 'host-session');
  assert.equal(result.identity.consumer_id, 'host-owner');
  assert.equal(result.identity.run_id, 'host-call');
});
test('missing trusted session fails before any subprocess', () => {
  assert.throws(() => trustedEnvelope({}, 'call', { request: '写通知' }), /会话身份/);
});
test('an unresolved request carries prior user context without rewriting the current message', () => {
  const ctx = { sessionId: 'host-session', sessionKey: 'host-key' };
  const params = { request: '日期是星期五。', prior_request: '帮我写一份培训通知。' };
  const result = trustedEnvelope(ctx, 'host-call', params);
  assert.equal(result.input.user_prompt, params.request);
  assert.equal(result.input.prior_request, params.prior_request);
  assert.equal(trustedEnvelope(ctx, 'host-call', { ...params, continue_from: 'parent' }).input.prior_request, undefined);
});
test('public schedule is separate from the user text and only enters new drafts', () => {
  const ctx = { sessionId: 'host-session', sessionKey: 'host-key' };
  const public_schedule = { url: 'https://example.org/calendar', quote: '会议于11月18日至19日举行' };
  const params = { request: '按会前一天、会后一天起草', prior_request: '写一份居家办公通知', public_schedule };
  const result = trustedEnvelope(ctx, 'host-call', params).input;
  assert.equal(result.user_prompt, params.request);
  assert.equal(result.prior_request, params.prior_request);
  assert.deepEqual(result.public_schedule, public_schedule);
  for (const extra of [{ continue_from: 'parent' }, { delivery_action: 'resend_word' }]) {
    assert.equal(trustedEnvelope(ctx, 'host-call', { ...params, ...extra }).input.public_schedule, undefined);
  }
});
test('a path from another session is rejected even under a shared inbound root', () => {
  const ctx = {sessionId: 'current', sessionKey: 'key'};
  const params = {request: '按附件写', attachments: ['/oasn/in/other.docx']};
  assert.throws(() => trustedEnvelope(ctx, 'call', params), /可信绑定/);
  assert.throws(() => trustedEnvelope(ctx, 'call', params,
    {session_id: 'other', input: {attachments: params.attachments}}), /可信绑定/);
  assert.deepEqual(trustedEnvelope(ctx, 'call', params,
    {session_id: 'current', input: {attachments: params.attachments}}).input.authorized_attachments, params.attachments);
});

test('a gateway turn consumes every host-recorded attachment, independent of model path suggestions', () => {
  const ctx = {sessionId: 'current', sessionKey: 'key'};
  const first = '/root/.openclaw/media/inbound/会商材料---upload.txt';
  const second = '/root/.openclaw/media/inbound/参考---upload.docx';
  const projected = {session_id: 'current', input: {attachments: ['/oasn/in/old.txt']}};
  for (const attachments of [undefined, ['/oasn/media/inbound/会商材料---upload.txt'], ['/oasn/in/old.txt']]) {
    const {input} = trustedEnvelope(ctx, 'call', {request: '按附件写', attachments}, projected, undefined, 'node', [first, second]);
    assert.deepEqual(input.attachments, [first, second]);
    assert.deepEqual(input.authorized_attachments, [first, second]);
  }
  assert.throws(() => trustedEnvelope(ctx, 'call', {request: '按附件写', attachments: ['/oasn/in/old.txt']},
    projected, undefined, 'next-node', []), /可信绑定/);
  assert.throws(() => trustedEnvelope(ctx, 'call', {request: '按附件写'},
    undefined, undefined, 'node', ['/root/.openclaw/media/inbound/../historical.txt']), /可信绑定/);
  assert.throws(() => trustedEnvelope(ctx, 'call', {request: '按附件写'},
    undefined, undefined, 'node', null), /可信绑定/);
});

test('one host turn cannot rewrite a failed request and start another generation', async () => {
  let factory;
  plugin.register({ on() {}, registerTool: (value, options) => {
    if (options.name === 'gongwen_write') factory = value;
  } });
  const firstTurn = factory({});
  assert.equal(firstTurn.parameters.additionalProperties, false);
  assert.equal(Object.hasOwn(firstTurn.parameters.properties, 'single_writer'), false);
  await assert.rejects(firstTurn.execute('call-one', {request: '9点培训'}), /会话身份/);
  const retry = await firstTurn.execute('call-two', {request: '9时培训，不写00'});
  assert.equal(retry.isError, true);
  assert.equal(retry.details.error_code, 'TURN_ALREADY_ATTEMPTED');
  assert.equal(retry.details.current_delivery.message, retry.content[0].text);
  assert.doesNotMatch(retry.content[0].text, /TURN_ALREADY_ATTEMPTED|调用|工具/);
  const nextTurn = factory({});
  await assert.rejects(nextTurn.execute('new-user-turn', {request: '用户下一轮修改'}), /会话身份/);
});

test('a success receipt needs exactly one matching attachment in the same delivery', () => {
  const output = '/oasn/out/gongwen/0123456789abcdef01234567';
  const message = 'Word写好了，可以直接修改。';
  const header = JSON.stringify({status: 'draft', output_directory: output,
    delivery_message: message, word_contents: {file_name: '培训通知.docx', table_count: 0}});
  const attachment = `MEDIA:${output}/交付/培训通知.docx`;
  const delivered = nativeDeliveryResult(`${header}\n${attachment}\n`, 0, 'test');
  assert.equal(delivered.isError, false);
  assert.equal(delivered.content[0].text, `${header}\n${attachment}`);
  for (const media of ['', `\n${attachment}\n${attachment}`, `\nMEDIA:${output}/交付/另一份.docx`,
    '\nMEDIA:/oasn/out/gongwen/1234567890abcdef01234567/交付/培训通知.docx']) {
    const result = nativeDeliveryResult(header + media, 0, 'test');
    assert.equal(result.isError, true);
    assert.doesNotMatch(result.content[0].text, /写好了|MEDIA:/);
  }
  const interrupted = nativeDeliveryResult(`${header}\n${attachment}`, null, 'test');
  assert.equal(interrupted.isError, true);
  assert.doesNotMatch(interrupted.content[0].text, /写好了|MEDIA:/);
});

test('a failed text-only receipt keeps its user-facing text and has no attachment', () => {
  const message = '本轮正文暂未完成。你可以沿用刚才的材料继续核对，或补充需要调整的地方。';
  for (const status of ['failed', 'needs_review']) {
    const result = nativeDeliveryResult(JSON.stringify({ status, word_contents: null,
      delivery_message: message }), 2, 'managed-invocation');
    assert.deepEqual(result.details.current_delivery, { files: [], message });
    assert.doesNotMatch(result.content[0].text, /Word|MEDIA:/);
  }
});

test('final delivery trusts an attachment only in the matching run and session', () => {
  const hooks = new Map();
  plugin.register({ on: (name, handler) => hooks.set(name, handler), registerTool() {} });
  const directory = '/oasn/out/gongwen/0123456789abcdef01234567';
  const file = directory + '/交付/培训通知.docx';
  const receipt = { status: 'draft', output_directory: directory,
    delivery_message: 'Word写好了，可以直接修改。', word_contents: { file_name: '培训通知.docx' } };
  const result = nativeDeliveryResult(JSON.stringify(receipt) + '\nMEDIA:' + file, 0, 'test');
  hooks.get('after_tool_call')({ toolName: 'gongwen_write', result, runId: 'current' }, { sessionKey: 'owner' });
  const staleReply = (runId, sessionKey) => ({ surface: 'webchat', runId, sessionKey,
    text: '旧链接', files: [{ url: '旧链接' }], payloads: [{ text: '旧链接' }],
    message: { text: '旧链接', content: [{ type: 'text', text: '旧链接' }] } });
  const finalize = hooks.get('final_reply_ready');
  for (const reply of [staleReply('other', 'owner'), staleReply('current', 'another-owner')]) {
    finalize(reply);
    assert.deepEqual(reply.files, []);
    assert.match(reply.message.text, /^本轮没有取得可下载的附件/);
    assert.deepEqual(reply.payloads, [{ text: reply.message.text }]);
  }
  const currentReply = staleReply('current', 'owner');
  finalize(currentReply);
  assert.equal(currentReply.files[0].url, file);
  assert.equal(currentReply.message.text, receipt.delivery_message);
  assert.deepEqual(currentReply.payloads, [{ text: receipt.delivery_message, mediaUrls: [file] }]);
  assert.equal(currentReply.files[0].sourceField, 'mediaUrls');
  assert.equal(currentReply.files[0].payloadIndex, 0);

  hooks.get('after_tool_call')({ toolName: 'gongwen_write', result, runId: 'json-run' }, { sessionKey: 'owner' });
  const jsonReply = { surface: 'local_json', runId: 'json-run', sessionKey: 'owner',
    text: '旧链接', files: [{ url: '旧链接' }], payloads: [{ text: '旧链接' }] };
  finalize(jsonReply);
  assert.equal(jsonReply.files[0].url, file);
  assert.equal(jsonReply.text, receipt.delivery_message);
  assert.deepEqual(jsonReply.payloads, [{ text: receipt.delivery_message, mediaUrls: [file] }]);
});

test('plain replies finish the current question without reciting the last turn or asking for materials again', () => {
  const hooks = new Map();
  plugin.register({ on: (name, handler) => hooks.set(name, handler), registerTool() {} });
  const previous = '给分管领导会前看，旧稿只参考结构，以这次纪要为准。';
  const current = '另外单独问一句，手头另一份 PDF 的字选不中，我现在能直接发这份 PDF 吗？';
  const answer = '另外那份字选不中的 PDF，请换成可选中文字的版本，或把文字贴过来。';
  const messages = [{ role: 'assistant', content: [{ type: 'text', text: previous }] }];
  const prepare = hooks.get('agent_turn_prepare');
  const finalize = hooks.get('final_reply_ready');
  const ctx = { sessionKey: 'consult', sessionId: 'session-consult', runId: 'current-question' };
  const original = '一句话确认：这份会商材料是给分管领导会前看的，旧稿只参考结构、以这次纪要为准。\n\n' + answer;
  prepare({ prompt: current, messages }, ctx);
  const reply = { surface: 'agent_command', ...ctx, text: original, files: [], payloads: [{ text: original }] };
  finalize(reply, ctx);
  assert.equal(reply.text, answer);
  assert.deepEqual(reply.payloads, [{ text: answer }]);

  const intake = { ...ctx, runId: 'intake' };
  prepare({ prompt: '先不用起稿，我怎么发材料？', messages }, intake);
  const instructions = '请把纪要和部门意见分开贴，旧稿标明只参考结构。';
  const soliciting = { surface: 'webchat', ...intake, text: instructions + '\n\n要不要现在就把材料贴过来？' };
  finalize(soliciting, intake);
  assert.equal(soliciting.text, instructions);

  const placement = '纪要定的进正文，部门提的进待议，两不混写。';
  const variants = [
    [placement + '等你把文字发来，我可以按这个框架直接排。', placement],
    [placement + '\n\n等你把材料贴过来，我就能按这个结构整理。', placement],
    [instructions + '\n\n这轮不起稿，你发材料时照上面贴就行。', instructions],
    ['这份 PDF 选不中字。手头有文字的话，可以直接贴给我。',
      '这份 PDF 选不中字。手头有文字的话，可以直接贴给我。'],
    ['请补一下会议日期。', '请补一下会议日期。'],
  ];
  for (const [text, expected] of variants) {
    const result = { surface: 'webchat', ...intake, text, payloads: [{ text }] };
    finalize(result, intake);
    assert.equal(result.text, expected);
    assert.deepEqual(result.payloads, [{ text: expected }]);
  }

  const requested = { ...ctx, runId: 'explicit-recap' };
  prepare({ prompt: '先确认一下刚才的读者和旧稿用途。另外再答一下 PDF。', messages }, requested);
  const recap = { surface: 'agent_command', ...requested, text: original };
  finalize(recap, requested);
  assert.equal(recap.text, original);
});

test('a requested excerpt or consultation ends without asking the user to start drafting', () => {
  const hooks = new Map();
  plugin.register({ on: (name, handler) => hooks.set(name, handler), registerTool() {} });
  const notice = '开头一句可以这样：\n\n「各位新同事，本周四下午三点在三楼会议室开展培训，请准时参加。」';
  const noticeCloser = '\n\n正文还差几项：培训讲什么主题、大概多长时间、谁主讲（或哪个部门组织）、需不需要带材料或提前准备。你把这几样定了，我就能接着写。';
  const brief = '先去掉套话，把第一段最重要的事提到前面，后面的数字和结论照原文保留。';
  const briefCloser = '\n\n你把简报开头那段发我，我就能具体指出哪几句可以删、哪几句要留。';
  const intake = '培训主题和预计时长最好写清楚。有相对日期就行，不一定要精确到几月几号。';
  const intakeCloser = '你把这些补齐，或者告诉我哪些可以留空，我就开始写。';
  const pdf = '字选不中，这份我暂时读不了正文，原因我不猜。你手头有能复制的文字版，就可以用它继续。';
  const mixed = '简报：我只动开头。\n\n会商：三份各管一摊。\n\nPDF：' + pdf;
  const cases = [
    ['时间改周四下午三点，其他照旧。群里发的话，开头怎么说一句就行？', notice + noticeCloser, notice],
    ['只想压短开头，后面的数字和结论都别动。先说说你会怎么改。', brief + briefCloser, brief],
    ['这份通知还缺什么？', notice + noticeCloser, notice + noticeCloser],
    ['我想发个培训通知，先不写正文，我还得交代什么？', intake + intakeCloser, intake],
    ['培训通知我还得交代什么？齐了就开始写。', intake + intakeCloser, intake + intakeCloser],
    ['先说说你会怎么改。我需要发什么？', brief + briefCloser, brief + briefCloser],
    ['PDF 的字选不中，这份能直接发吗？', '我暂时读不了这份 PDF。\n\n你把能复制的正文贴给我，我就能帮你整理。', '我暂时读不了这份 PDF。\n\n你把能复制的正文贴给我，我就能帮你整理。'],
    ['另外单独问一下，我手头还有一份 PDF，里面的字选不中，这份能直接发吗？', mixed, pdf],
    ['先总结一下简报和会商，再说这份选不中字的 PDF。', mixed, mixed],
  ];
  for (const [i, [prompt, text, expected]] of cases.entries()) {
    const ctx = { sessionKey: 'scoped-replies', sessionId: 'scoped-session', runId: String(i) };
    hooks.get('agent_turn_prepare')({ prompt, messages: [{ role: 'assistant' }] }, ctx);
    const reply = { surface: 'webchat', ...ctx, text, payloads: [{ text }] };
    hooks.get('final_reply_ready')(reply, ctx);
    assert.equal(reply.text, expected);
    assert.deepEqual(reply.payloads, [{ text: expected }]);
  }
});

test('managed final replies keep the verified result for both documents and failures', () => {
  const hooks = new Map();
  plugin.register({ on: (name, handler) => hooks.set(name, handler), registerTool() {} });
  const directory = '/oasn/out/gongwen/0123456789abcdef01234567';
  const file = directory + '/交付/当前通知.docx';
  for (const [status, code, document] of [['draft', 0, true], ['needs_input', 2, false],
    ['needs_review', 2, false], ['failed', 1, false], ['text_only', 0, false]]) {
    const message = status === 'failed' || status === 'needs_review' ? '内部排查编号 01' : '工具反馈';
    const receipt = { status, output_directory: directory, delivery_message: message,
      word_contents: document ? { file_name: '当前通知.docx' } : null };
    const header = JSON.stringify(receipt);
    const result = nativeDeliveryResult(document ? header + '\nMEDIA:' + file : header, code, 'managed-invocation');
    hooks.get('after_tool_call')({ toolName: 'gongwen_write', result,
      error: result.isError ? 'tool returned failure' : undefined, runId: status }, { sessionKey: 'current' });
    const finalize = hooks.get('final_reply_ready');
    const event = { surface: 'agent_command', runId: status, sessionKey: 'current',
      text: '助手自行生成的正文和旧链接', files: [{ url: '旧链接' }], payloads: [{ text: '旧链接' }] };
    finalize(event);
    assert.equal(event.text, result.details.current_delivery.message);
    assert.deepEqual(event.payloads, [document ? { text: event.text, mediaUrls: [file] } : { text: event.text }]);
    assert.doesNotMatch(event.text, /自行生成|排查编号/);
    assert.deepEqual(event.files.map(value => value.url), document ? [file] : []);
  }
});

test('an upstream failure never copies the internal reference into delivery text', () => {
  const header = JSON.stringify({status: 'failed', word_contents: null,
    delivery_message: '这次没生成 Word。模型服务请求失败，排查编号 gongwen-test。'});
  const failure = nativeDeliveryResult(header, 1, 'test');
  assert.equal(failure.isError, true);
  assert.match(failure.content[0].text, /暂时没能生成可用的 Word/);
  assert.doesNotMatch(failure.content[0].text, /gongwen-test|排查编号|MEDIA:/);
});

test('clarification is a normal tool result without an attachment', () => {
  const header = JSON.stringify({status: 'needs_input', word_contents: null,
    delivery_message: '请补充管制日期、时段和路段。'});
  const clarified = nativeDeliveryResult(header, 2, 'managed-invocation');
  assert.equal(clarified.isError, false);
  assert.equal(clarified.content[0].text, header);
  assert.equal(nativeDeliveryResult(header, null, 'managed-invocation').isError, true);
});

test('a blocked review forwards only diagnostics bound to its output directory', () => {
  const reference = '0123456789abcdef01234567';
  const output = `/oasn/out/gongwen/${reference}`;
  const diagnostic = {stage: 'task_evidence', code: 'REVIEW_BINDING_INVALID', reference};
  const receipt = {status: 'needs_review', output_directory: output, word_contents: null,
    delivery_message: '最终核验拦截，未生成 Word。', diagnostic};
  const blocked = nativeDeliveryResult(JSON.stringify(receipt), 2, 'managed-invocation');
  assert.equal(blocked.isError, true);
  assert.equal(blocked.details.failed_stage, diagnostic.stage);
  assert.equal(blocked.details.error_code, diagnostic.code);
  assert.equal(blocked.details.diagnostic_reference, reference);
  assert.doesNotMatch(JSON.parse(blocked.content[0].text).delivery_message, /核验|REVIEW_BINDING_INVALID|排查编号/);
  assert.match(JSON.parse(blocked.content[0].text).delivery_message, /查看未核定原稿/);
  for (const [stage, expected] of [['draft_check', '和原材料对上'], ['review', '独立核稿尚未完成'],
    ['repair', '需要改动的地方还没改妥'], ['task_evidence', '仍有内容没有核实']]) {
    const result = nativeDeliveryResult(JSON.stringify({...receipt, diagnostic: {...diagnostic, stage}}), 2, 'managed-invocation');
    assert.ok(JSON.parse(result.content[0].text).delivery_message.includes(expected));
  }
  assert.match(JSON.parse(blocked.content[0].text).delivery_message, /沿用刚才的材料/);
  const withFields = {...diagnostic, disputed_fields: ['acceptance', 'INTERNAL_MARKER', 'acceptance']};
  const scoped = nativeDeliveryResult(JSON.stringify({...receipt, diagnostic: withFields}), 2, 'managed-invocation');
  assert.match(JSON.parse(scoped.content[0].text).delivery_message, /需要核对验收方式/);
  assert.doesNotMatch(JSON.parse(scoped.content[0].text).delivery_message, /INTERNAL_MARKER|acceptance/);

  const mismatched = {...receipt, diagnostic: {...diagnostic, reference: 'a'.repeat(24)}};
  assert.equal(nativeDeliveryResult(JSON.stringify(mismatched), 2, 'managed-invocation')
    .details.diagnostic_reference, undefined);
  assert.equal(nativeDeliveryResult(`${JSON.stringify(receipt)}\nMEDIA:${output}/交付/公文.docx`, 2, 'managed-invocation')
    .details.error_code, 'WORD_DELIVERY_INCOMPLETE');
});

test('only explicit recovery can attach the original rejected draft, without claiming Word delivery', () => {
  const output = '/oasn/out/gongwen/0123456789abcdef01234567';
  const receipt = {status: 'recovered_draft', output_directory: output,
    delivery_message: '找回了当时被拦的原稿。原稿尚未通过核稿。', word_contents: null};
  const attachment = `MEDIA:${output}/交付/未通过核稿的原稿.txt`;
  const complete = `${JSON.stringify(receipt)}\n${attachment}`;
  assert.equal(nativeDeliveryResult(complete, 0, 'saved-delivery').content[0].text, complete);
  for (const bad of [JSON.stringify(receipt), complete + '\n' + attachment,
    complete.replace('未通过核稿的原稿.txt', '运行回执.txt'), complete.replace('recovered_draft', 'needs_review')]) {
    assert.equal(nativeDeliveryResult(bad, 0, 'saved-delivery').isError, true);
  }
});

test('managed input uses the host run projection even when the model rewrites this turn', () => {
  const run = 'run_' + '1'.repeat(32);
  const ctx = { sessionId: 'openclaw-session', sessionKey: 'owner' };
  const params = { request: '要 Word' };
  const projection = { session_id: 'platform-session', run_id: run, input: { user_prompt: '只要正文。' } };
  const envelope = trustedEnvelope(ctx, 'call', params, projection, run);
  assert.equal(envelope.input.user_prompt, '只要正文。');
  assert.equal(envelope.identity.run_id, run);
  assert.throws(() => trustedEnvelope(ctx, 'call', params, { ...projection, run_id: 'previous' }, run));
});

test('structured Word media enters the host projector only after the current receipt is accepted', () => {
  const path = '/oasn/out/gongwen/123456781234567812345678';
  const file = path + '/交付/当前.docx';
  const header = JSON.stringify({ status: 'draft', output_directory: path,
    delivery_message: '可以下载', word_contents: { file_name: '当前.docx' } });
  const allowed = nativeDeliveryResult(header + '\nMEDIA:' + file, 0, 'test');
  assert.deepEqual(allowed.details.media, { mediaUrls: [file], trustedLocalMedia: true });
  const refused = nativeDeliveryResult(header + '\nMEDIA:/oasn/out/其他任务/当前.docx', 0, 'test');
  assert.equal(refused.details.media, undefined);
  const hooks = new Map();
  plugin.register({ on: (k, f) => hooks.set(k, f), registerTool() {} });
  const ctx = { sessionKey: 'managed-session', oasnRuntimeEnv: { OASN_RUN_ID: 'managed-current' } };
  hooks.get('after_tool_call')({ toolName: 'gongwen_write', runId: 'inner-run', result: allowed }, ctx);
  const finalize = hooks.get('final_reply_ready');
  const event = () => ({ surface: 'webchat', sessionKey: ctx.sessionKey, runId: 'outer-client-run',
    text: '已发送附件 https://openagent.org/api/artifacts/old/content', files: [], payloads: [] });
  const right = event();
  finalize(right, ctx);
  assert.equal(right.text, '可以下载');
  assert.deepEqual(right.files.map(f => f.url), [file]);
  const wrong = event();
  finalize(wrong, { ...ctx, oasnRuntimeEnv: { OASN_RUN_ID: 'other-run' } });
  assert.match(wrong.text, /没有取得可下载/);
  assert.deepEqual(wrong.files, []);
});

test('standalone public dates use the source from this invocation', () => {
  const hooks = new Map();
  plugin.register({ on: (name, handler) => hooks.set(name, handler), registerTool() {} });
  const url = 'https://www.sz.gov.cn/example';
  const ctx = { sessionKey: 'agent:main:citations', runId: 'schedule' };
  const result = { content: [{ type: 'text', text: JSON.stringify({ status: 'results', sources: [
    { url, title: '政府公告', excerpts: ['会议于11月18日至19日举行。'] },
  ] }) }] };
  hooks.get('after_tool_call')({ toolName: 'gongwen_public_search', result, runId: ctx.runId }, ctx);
  const event = { ...ctx, surface: 'local_json', text: '会议于11月18日至19日举行。', files: [], payloads: [] };
  const other = { ...event, runId: 'unrelated' };
  hooks.get('final_reply_ready')(other, ctx);
  assert.equal(other.text, '会议于11月18日至19日举行。');
  hooks.get('final_reply_ready')(event, ctx);
  assert.ok(event.text.includes(url));
});
