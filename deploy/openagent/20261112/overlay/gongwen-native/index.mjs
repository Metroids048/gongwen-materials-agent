import { spawn } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { posix as path } from 'node:path';
import { currentCommandEnvironment, loadRuntimeScope } from './runtime-environment.mjs';
import { publicSearchTool } from './public-search.mjs';
import { registerCurrentDelivery, rememberCurrentDelivery, rememberReplyTurn } from './current-delivery.mjs';
import { readUserTurn } from './user-turn.mjs';

const wordNotReady = '这次暂时没能生成可用的 Word。你刚才的要求和补充信息都在这段对话里。';
const reviewNotReady = '这轮还没有通过核稿，暂时没有可下载的 Word。';
const fieldNames = new Map(Object.entries({ action: '行动', lead: '牵头单位', support: '配合单位',
  timing: '办理时间', deliverable: '交付成果', acceptance: '验收方式' }));
const savedDraftOptions = '你可以说「查看未核定原稿」，先看这一轮留存的文字，也可以让我沿用刚才的材料继续。';
const heldDraftMessages = {
  draft_check: '初稿已留存，有内容还没能和原材料对上，暂时没有可下载的 Word。',
  review: '初稿已留存，独立核稿尚未完成，暂时没有可下载的 Word。',
  repair: '初稿和核稿意见已留存，需要改动的地方还没改妥，暂时没有可下载的 Word。',
  task_evidence: '稿件已留存，仍有内容没有核实，暂时没有可下载的 Word。',
};
const textNotReady = '本轮正文暂未完成。你可以沿用刚才的材料继续核对，或补充需要调整的地方。';
const previousFailure = '原调用已失败，未生成可交付文件。可以在当前对话中继续这份材料。';
const gatewayDocumentState = { turns: new Map(), calls: new Map() };
const inboundRoots = new Set(['/root/.openclaw/media/inbound', '/oasn/.openclaw/media/inbound']);
const attachmentNotBound = '这轮没能确认收到你的附件。请用「附加文件」把原件和要求一同重发。';
class AttachmentBindingError extends Error {}

// A gateway can recreate the tool factory between model steps of the same run.
// Bind the original prompt and the first business result outside those factories.
export function registerDocumentTurns(api, resolveUserTurn = readUserTurn, state = { turns: new Map(), calls: new Map() }) {
  const { turns, calls } = state;
  const key = (sessionKey, sessionId, id) => JSON.stringify([sessionKey, sessionId, id]);
  function bind(id, prompt, userNodeId, mediaPaths) {
    if (turns.has(id)) return turns.get(id);
    for (const [oldId, turn] of turns) {
      if (turn.expires > Date.now() && turns.size < 128) break;
      turns.delete(oldId);
      for (const call of turn.calls) calls.delete(call);
    }
    const turn = { prompt, userNodeId, mediaPaths, calls: new Set(), expires: Date.now() + 3_600_000 };
    turns.set(id, turn);
    return turn;
  }
  api.on('agent_turn_prepare', (event, ctx) => {
    if (!ctx.runId || !ctx.sessionKey || !ctx.sessionId || typeof event.prompt !== 'string' || !event.prompt.trim()) return;
    rememberReplyTurn(event, ctx);
    bind(key(ctx.sessionKey, ctx.sessionId, ctx.runId), event.prompt);
  });
  api.on('before_tool_call', (event, ctx) => {
    if (event.toolName !== 'gongwen_write' || !ctx.sessionKey || !ctx.sessionId || !event.toolCallId || !event.runId) return;
    const turn = turns.get(key(ctx.sessionKey, ctx.sessionId, event.runId));
    if (!turn) return;
    const call = key(ctx.sessionKey, ctx.sessionId, event.toolCallId);
    calls.set(call, turn);
    turn.calls.add(call);
  });
  return (ctx, callId, execute) => {
    function replay(turn) {
      if (!turn) return execute(undefined);
      if (!turn.result) turn.result = Promise.resolve().then(() => execute(turn.prompt, turn.userNodeId, turn.mediaPaths));
      return turn.result;
    }
    async function replayManaged() {
      let run;
      try { run = (await loadRuntimeScope()).ownedRuntimeContext()?.environment?.OASN_RUN_ID; } catch {}
      return replay(/^run_[a-f0-9]{32}$/.test(run ?? '')
        ? bind(key(ctx.sessionKey, ctx.sessionId, 'managed:' + run), undefined) : undefined);
    }
    // WebChat and direct gateway sessions persist the original user message
    // even on hosts that omit the prompt hook. This excludes transport prompts.
    if (/^agent:[a-z0-9_-]+:/.test(ctx.sessionKey ?? '') && /^[a-f0-9-]{36}$/i.test(ctx.sessionId)) {
      return resolveUserTurn(ctx, callId).then(source => source
        ? replay(bind(key(ctx.sessionKey, ctx.sessionId, 'user:' + source.id), source.prompt, source.id, source.mediaPaths)) : replayManaged());
    }
    const hooked = calls.get(key(ctx.sessionKey, ctx.sessionId, callId));
    return hooked || !ctx.sessionId || !ctx.sessionKey ? replay(hooked) : replayManaged();
  };
}

export function trustedEnvelope(ctx, callId, params, projection, managedRunId, userNodeId, mediaPaths) {
  if (!ctx.sessionId || !ctx.sessionKey || !callId) throw new Error('缺少运行时会话身份，未调用模型。');
  if (typeof params.request !== 'string' || !params.request.trim()) throw new Error('请提供本轮公文需求。');
  const requested = params.attachments ?? [];
  const managed = typeof managedRunId === 'string' && /^run_[a-f0-9]{32}$/.test(managedRunId);
  const bound = managed ? projection?.run_id === managedRunId : projection?.session_id === ctx.sessionId;
  if (managed && (!bound || typeof projection.input?.user_prompt !== 'string' || !projection.input.user_prompt.trim())) {
    throw new Error('没有取得这一次的原始消息，请在当前对话继续。');
  }
  const gatewayTurn = !managed && Boolean(userNodeId);
  if (gatewayTurn && (!Array.isArray(mediaPaths) || mediaPaths.some(file => typeof file !== 'string' ||
      !inboundRoots.has(path.dirname(file)) || path.normalize(file) !== file || file.includes('\0') ||
      ['.', '..'].includes(path.basename(file)) || file.endsWith('/')))) {
    throw new AttachmentBindingError('网关附件未取得本轮可信绑定。');
  }
  const authorized = gatewayTurn ? [...new Set(mediaPaths)] : bound ? projection.input?.attachments ?? [] : [];
  // Use the current user node's complete upload list, including files the router omitted
  // or referred to by a sandbox alias. An empty user node never inherits earlier uploads.
  const attachments = gatewayTurn && authorized.length ? authorized : requested;
  if (!Array.isArray(attachments) || attachments.some(file => typeof file !== 'string' || !authorized.includes(file))) {
    throw new AttachmentBindingError('附件尚未取得本轮可信绑定。');
  }
  const input = { user_prompt: managed ? projection.input.user_prompt : params.request, attachments, authorized_attachments: authorized };
  if (!params.continue_from && typeof params.prior_request === 'string' && params.prior_request.trim()) {
    input.prior_request = params.prior_request;
  }
  if (!params.continue_from && !params.delivery_action && params.public_schedule) {
    const { url, quote } = params.public_schedule;
    if (typeof url === 'string' && url.length <= 2048 && typeof quote === 'string' && quote.length <= 1200) {
      input.public_schedule = { url, quote };
    }
  }
  return { identity: { session_id: ctx.sessionId, consumer_id: ctx.requesterSenderId ?? ctx.sessionKey,
    invocation_id: callId, run_id: managed ? managedRunId : (userNodeId ? 'user:' + userNodeId : callId) }, input };
}

export function nativeDeliveryResult(stdout, code, source) {
  const details = { adapter_revision: 'r16', environment_source: source, exit_code: code };
  const incomplete = { content: [{ type: 'text', text: wordNotReady }],
    details: { ...details, error_code: 'WORD_DELIVERY_INCOMPLETE',
      current_delivery: { files: [], message: wordNotReady } }, isError: true };
  const [header, ...media] = stdout.trimEnd().split('\n');
  let receipt;
  try { receipt = JSON.parse(header); } catch { return incomplete; }
  if (typeof receipt?.status !== 'string' || typeof receipt.delivery_message !== 'string') return incomplete;
  const directory = receipt.output_directory;
  const recovered = receipt.status === 'recovered_draft';
  const files = media.map(line => line.slice('MEDIA:'.length));
  if (files.length && (!/^\/oasn\/out\/gongwen\/[0-9a-f]{24}$/.test(directory) ||
      new Set(files).size !== files.length || media.some((line, index) => {
        const file = files[index];
        return !line.startsWith('MEDIA:') || !file.startsWith(`${directory}/`) ||
          path.normalize(file) !== file || path.extname(file) !== (recovered ? '.txt' : '.docx');
      }))) return incomplete;
  const word = receipt.word_contents?.file_name;
  if (recovered && (receipt.word_contents !== null || files.length !== 1 ||
      files[0] !== `${directory}/交付/未通过核稿的原稿.txt`)) return incomplete;
  if ((word || ['draft', 'revision'].includes(receipt.status)) &&
      (!word || files.filter(file => path.basename(file) === word).length !== 1)) return incomplete;
  if (files.length && ['failed', 'needs_input', 'needs_review', 'text_only'].includes(receipt.status)) return incomplete;
  const deliveredReview = code === 2 && receipt.status === 'held' && Boolean(word);
  const clarification = code === 2 && receipt.status === 'needs_input' && receipt.word_contents === null;
  if (clarification) details.needs_user_input = true;
  if (files.length && code !== 0 && !deliveredReview) return incomplete;
  const diagnostic = receipt.diagnostic;
  if (receipt.status === 'needs_review' && diagnostic &&
      /^(draft_check|review|repair|task_evidence)$/.test(diagnostic.stage) &&
      /^[A-Z][A-Z0-9_]{0,47}$/.test(diagnostic.code) &&
      /^[a-f0-9]{24}$/.test(diagnostic.reference) &&
      directory === `/oasn/out/gongwen/${diagnostic.reference}`) {
    Object.assign(details, { failed_stage: diagnostic.stage, error_code: diagnostic.code,
      diagnostic_reference: diagnostic.reference });
  }
  let publicHeader = header;
  let publicMessage = receipt.delivery_message;
  const disputed = details.failed_stage === 'task_evidence' && Array.isArray(diagnostic?.disputed_fields)
    ? [...new Set(diagnostic.disputed_fields.map(key => fieldNames.get(key)).filter(Boolean))] : [];
  const heldMessage = disputed.length
    ? `稿件已留存。返修后的分工表与材料对不上，需要核对${disputed.join('、')}，这轮还没有可下载的 Word。`
    : heldDraftMessages[details.failed_stage] ?? reviewNotReady;
  if (receipt.status === 'needs_review' || (receipt.status === 'failed' && source !== 'saved-delivery')) {
    if (receipt.delivery_message !== textNotReady && receipt.delivery_message !== previousFailure) {
      publicMessage = receipt.status === 'needs_review'
        ? heldMessage + savedDraftOptions
        : wordNotReady;
    }
    publicHeader = JSON.stringify({ status: receipt.status, word_contents: null,
      delivery_message: publicMessage,
      ...(receipt.status === 'needs_review' && details.diagnostic_reference ? { output_directory: directory } : {}) });
  }
  if (files.length || clarification || receipt.status === 'needs_review' || receipt.status === 'failed' ||
      (code === 0 && receipt.status === 'text_only' && receipt.word_contents === null)) {
    details.current_delivery = { files, message: publicMessage };
  }
  if (files.length) details.media = { mediaUrls: files, trustedLocalMedia: true };
  return { content: [{ type: 'text', text: [publicHeader, ...media].join('\n') }], details,
    isError: receipt.status === 'failed' || (code !== 0 && !deliveredReview && !clarification) };
}

export default {
  id: 'gongwen-native', name: '公文材料业务工具',
  register(api) {
    registerCurrentDelivery(api);
    const executeOnce = registerDocumentTurns(api, readUserTurn, gatewayDocumentState);
    api.registerTool(ctx => {
      // Fallback for hosts without turn hooks; managed input is separately bound on disk.
      let attempted = false;
      return ({
      name: 'gongwen_write', label: '生成或核改公文 Word',
      description: '起草、核稿、续改或找回本会话原件。完整传入本轮用户原话和明确附件。普通起草一次主笔。用户明确说“请对刚才的首稿独立核稿并返回Word”时，只传原话，程序从本会话查取首稿。用户明确说补发已有Word或找回被拦稿时，传delivery_action及相应历史工具回执的continue_from。新起草不传父版。needs_input 时向用户转达追问。needs_review 时转达 delivery_message。只交付本轮 MEDIA: 附件，不自行补写未核定正文或引用历史链接。',
      parameters: { type: 'object', additionalProperties: false, required: ['request'], properties: {
        request: { type: 'string', description: '本轮用户原话，保留全部事实和限制' },
        prior_request: { type: 'string', description: '仅当本轮是在补齐尚未成稿的同一需求时，填此前相关的用户原话（按先后顺序，包含起草要求和仍有效的事实）。不含助手回复、其他任务或旧稿。' },
        public_schedule: { type: 'object', additionalProperties: false, required: ['url', 'quote'], properties: {
          url: { type: 'string', description: '已打开并核对的官方会期公告 HTTPS 原始链接' },
          quote: { type: 'string', description: '公告中与本次所指活动匹配的原文和明确会期。原样摘录，不推测、不提供链接标题或搜索摘要替代原文。' }
        }, description: '仅在起草需要核实公开会期时填写。只支持证明公开会期，不能证明单位内部或公众执行安排。' },
        attachments: { type: 'array', items: { type: 'string' }, description: '网页本轮上传由程序直接读取。其他入口仅可填写本轮明确提供的附件路径。' },
        continue_from: { type: 'string', description: '仅明确续改或找回原件时填写本会话对应工具回执的output_directory；新起草或重发完整需求必须省略' },
        delivery_action: { type: 'string', enum: ['resend_word', 'recover_draft'], description: '仅明确找回文件时填写。resend_word补发已生成的正文字节，recover_draft找回被拦的原稿文本。' },
        opinion_template: { type: 'string', description: '本轮附件中的授权意见模板' }
      } },
      execute(callId, params, signal, onUpdate) {
        return executeOnce(ctx, callId, (rawPrompt, userNodeId, mediaPaths) =>
          executeDocument(callId, params, signal, onUpdate, rawPrompt, userNodeId, mediaPaths));
      }
      });
      async function executeDocument(callId, params, signal, onUpdate, rawPrompt, userNodeId, mediaPaths) {
        if (attempted) {
          const message = '这轮还没有可交付的内容。你可以在当前对话里接着刚才的要求继续。';
          return { content: [{ type: 'text', text: message }], details: { adapter_revision: 'r16',
            error_code: 'TURN_ALREADY_ATTEMPTED', current_delivery: { files: [], message } }, isError: true };
        }
        attempted = true;
        if (!ctx.sessionId || !ctx.sessionKey) throw new Error('缺少运行时会话身份，未调用模型。');
        let scopedRunId;
        const python = '/oasn/gongwen/.venv/bin/python';
        let executable = python;
        let argv = ['/oasn/gongwen/openagent_entry.py', '--native-input'];
        let runtime = { source: 'saved-delivery' };
        if (params.delivery_action) {
          argv.push('--delivery-action', params.delivery_action);
          try { scopedRunId = (await loadRuntimeScope()).ownedRuntimeContext()?.environment?.OASN_RUN_ID; } catch {}
        } else {
          try {
            // Resolve inside the host's AsyncLocalStorage scope at execution time.
            const scope = await loadRuntimeScope();
            runtime = { ...currentCommandEnvironment(scope), emitDiagnosticEvent: scope.emitDiagnosticEvent };
          } catch {
            return { content: [{ type: 'text', text: wordNotReady }],
              details: { adapter_revision: 'r16', error_code: 'RUNTIME_CONTEXT_UNAVAILABLE',
                current_delivery: { files: [], message: wordNotReady } }, isError: true };
          }
          if (runtime.source === 'managed-invocation') {
            scopedRunId = runtime.environment.OASN_RUN_ID;
          }
          executable = 'openagent-models-exec';
          argv = ['--', python, ...argv];
        }
        let projection;
        if (scopedRunId && !/^run_[a-f0-9]{32}$/.test(scopedRunId)) {
          return nativeDeliveryResult('', null, runtime.source);
        }
        if (scopedRunId || (!userNodeId && params.attachments?.length)) {
          const inputPath = scopedRunId ? `/oasn/.oasn-shared-runs/${scopedRunId}/in/invocation.json` : '/oasn/in/invocation.json';
          try { projection = JSON.parse(await readFile(inputPath, 'utf8')); } catch {}
        }
        if (!scopedRunId && !rawPrompt) {
          const message = '这轮没能取得你发来的原话。请在当前对话重发刚才的要求。';
          return { content: [{ type: 'text', text: message }], details: { error_code: 'CURRENT_USER_INPUT_UNAVAILABLE',
            current_delivery: { files: [], message } }, isError: true };
        }
        let envelope;
        try {
          envelope = trustedEnvelope(ctx, callId, rawPrompt ? { ...params, request: rawPrompt } : params,
            projection, scopedRunId, userNodeId, mediaPaths);
        } catch (error) {
          if (!(error instanceof AttachmentBindingError)) throw error;
          return { content: [{ type: 'text', text: attachmentNotBound }],
            details: { error_code: 'CURRENT_ATTACHMENT_UNBOUND',
              current_delivery: { files: [], message: attachmentNotBound } } };
        }
        if (params.continue_from) argv.push('--continue-from', params.continue_from);
        if (params.opinion_template) {
          const upload = userNodeId && !scopedRunId && envelope.input.attachments.find(file =>
            path.join('/oasn/media/inbound', path.basename(file)) === params.opinion_template);
          argv.push('--opinion-template', upload || params.opinion_template);
        }
        return await new Promise(resolve => {
          const child = spawn(executable, argv, { cwd: '/oasn/gongwen',
            env: runtime.environment, stdio: ['pipe', 'pipe', 'pipe'], signal });
          // A live subprocess can be waiting for a managed model for several minutes.
          // Keep the host tool lifecycle active while its promised result is pending.
          const updates = (onUpdate || runtime.emitDiagnosticEvent) && setInterval(() => {
            runtime.emitDiagnosticEvent?.({ type: 'run.progress', sessionId: ctx.sessionId,
              reason: 'tool:gongwen_write:waiting_for_worker' });
            onUpdate?.({ content: [{ type: 'text', text: '公文仍在处理，正在等待起草或核稿结果。' }] });
          }, 30_000).unref();
          let out = '';
          child.stdout.setEncoding('utf8').on('data', chunk => { out += chunk; });
          child.stderr.resume();
          child.once('error', () => {
            clearInterval(updates);
            resolve(nativeDeliveryResult('', null, runtime.source));
          });
          child.once('close', code => {
            clearInterval(updates);
            const result = nativeDeliveryResult(out, code, runtime.source);
            rememberCurrentDelivery(result, ctx, scopedRunId);
            if (result.details.needs_user_input) {
              onUpdate?.({ content: [{ type: 'text', text: result.details.current_delivery.message }] });
            }
            resolve(result);
          });
          child.stdin.on('error', () => {});
          child.stdin.end(JSON.stringify(envelope));
        });
      }
    }, { name: 'gongwen_write' });
    api.registerTool(publicSearchTool, { name: 'gongwen_public_search' });
  }
};
