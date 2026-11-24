import { createHash } from 'node:crypto';
import { appendFileSync, existsSync } from 'node:fs';

const wordMime = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';
const pending = new Map();
const citations = new Map();
const replyTurns = new Map();
const supportedSurfaces = new Set(['webchat', 'local_json', 'agent_command']);
const unconfirmed = '本轮没有取得可下载的附件。你可以在当前对话要求补发刚才生成的 Word。';
const unsolicitedClosers = [
  /\n\n[ \t]*(?:你)?要不要(?:现在)?(?:就)?把(?:这几份)?材料(?:贴|发)过来[？?][ \t]*$/,
  /(?<=[。！？\n])\s*等你把(?:这几份)?(?:材料|文字)(?:发|贴)(?:过来|来)[，,]\s*我(?:就能|可以|就会|就)按(?:这个|上面(?:的)?)(?:框架|结构|格式)(?:直接)?(?:排|整理|写|处理)[。！]?\s*$/,
  /\n\n\s*(?:这轮不起稿[，,]\s*)?你发材料时(?:就)?照上面(?:贴|发)就行[。！]?\s*$/,
];
const excerptFollowup = /\n\n[ \t]*(?:正文|整篇|完整(?:稿件|通知|正文))(?:还差|还缺|还需要|还得补)[^\n]+\s*$/;
const draftingInvitation = /\n\n[ \t]*(?:你(?:可以|先)?把|把)[^。\n]{1,45}(?:发|贴)(?:给我|我|过来|来)[，,]\s*我(?:就能|就可以|再|就)[^\n]+\s*$/;
const startDrafting = /(?<=[。！？\n])[ \t]*(?:你)?把(?:这几(?:样|项)|这些|上述(?:信息|事项))补(?:齐|好)[^。！？\n]{0,50}[，,]\s*我就(?:能|可以)?(?:开始|接着|继续)(?:写|起草|起稿)[。！]?\s*$/;

export function rememberReplyTurn(event, ctx) {
  if (!ctx?.sessionKey || !ctx.runId || typeof event.prompt !== 'string') return;
  const prompt = event.prompt.trim();
  const standalone = /^(?:(?:另外|再)\s*)?单独问(?:一句|一下|个问题)?[，、：:\s]/.test(prompt);
  const separate = (standalone || /^换个问题[，、：:\s]/.test(prompt)) &&
    Array.isArray(event.messages) && event.messages.some(message => message?.role === 'assistant');
  const pdfOnly = standalone && /\bPDF\b/i.test(prompt) && /选不中|选不出(?:文字|字)|(?:不能|没法)选中(?:文字|字)/.test(prompt);
  const excerpt = /(?:一句(?:话)?就(?:行|好)|只要一句|就写一句)[。！？?!\s]*$/.test(prompt);
  const consultation = /(?:先|只)(?:想听|想知道|说说|说一下|说|讲讲)(?:你)?(?:会)?怎么(?:改|写|整理|处理|安排)[。！？?!\s]*$/.test(prompt);
  const holdDraft = /(?:先|暂时|这轮)(?:不|不用|别)(?:写(?:正文|全文)?|起稿|起草)(?=[，。！？、\s]|$)/.test(prompt);
  if (!separate && !excerpt && !consultation && !holdDraft) return;
  if (replyTurns.size >= 256) replyTurns.delete(replyTurns.keys().next().value);
  replyTurns.set(JSON.stringify([ctx.sessionKey, ctx.runId]), {
    separate, pdfOnly, excerpt, consultation, holdDraft, expires: Date.now() + 600_000,
  });
}

function trace(phase, session, run, managed, count) {
  if (!existsSync('/oasn/gongwen/openagent_entry.py')) return;
  const digest = x => x ? createHash('sha256').update(String(x)).digest('hex').slice(0, 12) : undefined;
  try {
    appendFileSync('/oasn/out/gongwen/.native-delivery-trace.jsonl', JSON.stringify({
      at: new Date().toISOString(), phase, session: digest(session), run: digest(run), managed: digest(managed), count,
    }) + '\n', { mode: 0o600 });
  } catch {}
}

export function rememberCurrentDelivery(result, ctx, managed, run = ctx?.runId) {
  const delivery = result?.details?.current_delivery;
  if (!delivery || !ctx?.sessionKey) return;
  const now = Date.now();
  for (const [key, entry] of pending) if (entry.expires <= now) pending.delete(key);
  const ids = [run, managed].filter(Boolean);
  for (const id of ids) {
    if (pending.size >= 256) pending.delete(pending.keys().next().value);
    pending.set(JSON.stringify([ctx.sessionKey, id]), { delivery, expires: now + 600_000 });
  }
  trace('receipt', ctx.sessionKey, run, managed, delivery.files.length);
}

function replaceReply(event, { files, message }) {
  event.files = files.map(url => ({
    url, kind: 'file', sourceField: 'mediaUrls', payloadIndex: 0,
    mime_type: url.endsWith('.docx') ? wordMime : 'text/plain',
  }));
  event.text = event.displayReply = message;
  if (event.message) {
    event.message.text = message;
    event.message.content = [{ type: 'text', text: message }];
  }
  if (Array.isArray(event.payloads)) event.payloads.splice(0, event.payloads.length,
    files.length ? { text: message, mediaUrls: files } : { text: message });
}

export function registerCurrentDelivery(api) {
  api.on('after_tool_call', (event, ctx) => {
    const run = event.runId ?? ctx?.runId;
    const managed = ctx?.oasnRuntimeEnv?.OASN_RUN_ID;
    if (event.toolName === 'gongwen_write') rememberCurrentDelivery(event.result, ctx, managed, run);
    if (event.toolName !== 'gongwen_public_search' || !ctx?.sessionKey) return;
    let sources;
    try { sources = JSON.parse(event.result?.content?.[0]?.text).sources; } catch { return; }
    if (!Array.isArray(sources)) return;
    for (const id of [run, managed].filter(Boolean)) {
      if (citations.size >= 128) citations.delete(citations.keys().next().value);
      citations.set(JSON.stringify([ctx.sessionKey, id]), sources);
    }
  });
  // The official projector reads this event after awaiting its current invocation environment.
  api.on('final_reply_ready', (event, ctx) => {
    if (!supportedSurfaces.has(event.surface)) return;
    const session = event.sessionKey ?? ctx?.sessionKey;
    const managed = ctx?.oasnRuntimeEnv?.OASN_RUN_ID;
    const ids = [managed, event.runId ?? ctx?.runId].filter(Boolean);
    for (const id of ids) {
      const entry = pending.get(JSON.stringify([session, id]));
      if (!entry || entry.expires <= Date.now()) continue;
      replaceReply(event, entry.delivery);
      trace('final-bound', session, event.runId, managed, entry.delivery.files.length);
      return;
    }
    trace('final-unbound', session, event.runId, managed, event.files?.length ?? 0);
    // A model can repeat an old upload URL from the same long conversation.
    // Only the platform may mint a download URL after this draft's verified tool result.
    const prose = [event.text, event.displayReply, event.message?.text,
      ...(event.payloads ?? []).map(p => p.text)].join('\n');
    const unboundFiles = (event.files ?? []).length || (event.payloads ?? []).some(
      p => p.mediaUrl || p.mediaUrls?.length);
    if (unboundFiles || /\/api\/(?:artifacts\/|developer-workspaces\/[^\s/]+\/files\/)|MEDIA:\s*\/oasn\/out\/|已发送附件/.test(prose)) {
      replaceReply(event, { files: [], message: unconfirmed });
      return;
    }
    let text = event.text ?? event.displayReply ?? event.message?.text ?? '';
    if (text && (!event.payloads || event.payloads.length <= 1) && !event.files?.length) {
      let cleaned = text;
      for (const closer of unsolicitedClosers) cleaned = cleaned.replace(closer, '');
      const turn = ids.map(id => replyTurns.get(JSON.stringify([session, id])))
        .find(entry => entry?.expires > Date.now());
      if (turn?.separate) {
        cleaned = cleaned.replace(/^一句话确认[：:][^\n]+\n\n(?=另外|这份|那份)/, '');
        if (turn.pdfOnly) {
          const heading = cleaned.match(/^(?:#{1,6}[ \t]+)?(?:\*\*)?PDF(?:[ \t]+文件)?[：:](?:\*\*)?[ \t]*/im);
          if (heading && cleaned.slice(0, heading.index).includes('\n\n')) {
            cleaned = cleaned.slice(heading.index + heading[0].length);
          }
        }
      }
      if (turn?.excerpt) cleaned = cleaned.replace(excerptFollowup, '');
      if (turn?.excerpt || turn?.consultation || turn?.holdDraft) cleaned = cleaned.replace(draftingInvitation, '');
      if (turn?.holdDraft) cleaned = cleaned.replace(startDrafting, '');
      if (cleaned.trim() && cleaned !== text) {
        cleaned = cleaned.trimEnd();
        replaceReply(event, { files: [], message: cleaned });
        text = cleaned;
      }
    }
    if (event.files?.length || !text || /https?:\/\//i.test(text)) return;
    const dates = text.match(/\d{1,2}月\d{1,2}日/g) ?? [];
    if (!dates.length) return;
    for (const id of ids) {
      const sources = citations.get(JSON.stringify([session, id])) ?? [];
      const source = sources.find(item => {
        try {
          const url = new URL(item.url);
          return url.protocol === 'https:' && /(?:^|\.)(?:gov\.cn|apec\.org)$/.test(url.hostname)
            && dates.every(date => item.excerpts?.some(quote => quote.includes(date)));
        } catch { return false; }
      });
      if (source) {
        const title = String(source.title ?? '').replace(/[\[\]\n\r]/g, '').slice(0, 180);
        replaceReply(event, { files: [], message: text + '\n\n来源见[' + (title || '公开公告') + '](' + source.url + ')。' });
        return;
      }
    }
  }, { priority: 100 });
}
