import { readFile } from 'node:fs/promises';
import { loadRuntimeScope } from './runtime-environment.mjs';
import { readUserTurn } from './user-turn.mjs';

const endpoint = 'https://search.parallel.ai/mcp';
const searches = new Map();

export function needsPublicSchedule({ prompt, alsoWriting }) {
  if (/(?:不用|无需|不要|不必|先别)(?:再)?(?:上网|联网|查询|搜索|查会期)/.test(prompt)) return false;
  if (/(?:请|帮我|需要|顺便|先)?(?:查一下|查清|查询|查证|查一查|核实|核对|查找|查明|搜索|联网)|(?:何时|几号|哪一[天日]|哪几[天日]|哪天|什么时间|什么时候|有官方.{0,8}(?:来源|链接))/.test(prompt)) return true;
  if (alsoWriting || /相对(?:日期|表述|时间)|(?:前[一1]天|前[一1]日).{0,30}(?:后[一1]天|后[一1]日)/s.test(prompt)) return false;
  return true;
}

async function searchUserTurn(ctx, callId) {
  let run;
  try { run = (await loadRuntimeScope()).ownedRuntimeContext()?.environment?.OASN_RUN_ID; } catch {}
  const turn = await readUserTurn(ctx, callId, undefined, 'gongwen_public_search');
  if (!/^run_[a-f0-9]{32}$/.test(run ?? '')) return turn;
  try {
    const input = JSON.parse(await readFile(`/oasn/.oasn-shared-runs/${run}/in/invocation.json`, 'utf8'));
    const prompt = input.input?.user_prompt;
    if (input.run_id === run && typeof prompt === 'string' && prompt.trim()) {
      return { id: run, prompt, alsoWriting: turn?.alsoWriting };
    }
  } catch {}
}

export function publicSearchTool(ctx, resolveTurn = searchUserTurn) {
  const result = (status, extra = {}) => ({ content: [{ type: 'text', text: JSON.stringify({ status, ...extra }) }] });
  return {
    name: 'gongwen_public_search',
    label: '检索公开活动会期',
    description: '按公开活动名称和可选主办城市寻找官方会期。只传公开关键词，不传附件、单位内部安排、个人信息或聊天原文。',
    parameters: { type: 'object', additionalProperties: false, required: ['event'], properties: {
      event: { type: 'string', description: '公开活动的名称及可选年份，例如 2026 APEC 领导人非正式会议' },
      city: { type: 'string', description: '公开主办城市，例如 深圳' },
    } },
    async execute(callId, { event, city = '' }, signal) {
      const turn = await resolveTurn(ctx, callId);
      if (!turn || !needsPublicSchedule(turn)) {
        return result('not_needed', { message: '请沿用本轮业务补问，等待用户确认其安排。' });
      }
      if (typeof event !== 'string' || !/^[\p{L}\p{N} ()（）·–-]{2,90}$/u.test(event) ||
          typeof city !== 'string' || !/^[\p{L} ·-]{0,30}$/u.test(city) || /\d{5,}/.test(event)) {
        return result('invalid_public_keywords');
      }
      const key = JSON.stringify([ctx.sessionKey, ctx.sessionId, turn.id]);
      const previous = searches.get(key);
      if (previous) return previous;
      if (searches.size >= 128) searches.delete(searches.keys().next().value);
      const search = query(event, city, signal);
      searches.set(key, search);
      return search;
    },
  };
  async function query(event, city, signal) {
    const timeout = AbortSignal.timeout(12_000);
    const headers = { 'Content-Type': 'application/json', Accept: 'application/json, text/event-stream' };
    const request = async (method, params, id) => {
      const response = await fetch(endpoint, {
        method: 'POST', headers, redirect: 'error',
        signal: signal ? AbortSignal.any([timeout, signal]) : timeout,
        body: JSON.stringify({ jsonrpc: '2.0', method, params, id }),
      });
      if (!response.ok) throw new Error('Public search unavailable');
      const session = response.headers.get('Mcp-Session-Id');
      if (session) headers['Mcp-Session-Id'] = session;
      const body = await response.text();
      if (id === undefined) return;
      const payload = response.headers.get('Content-Type')?.includes('text/event-stream')
        ? body.split(/\r?\n/).find(line => line.startsWith('data: '))?.slice(6)
        : body;
      return JSON.parse(payload).result;
    };
    try {
      await request('initialize', { protocolVersion: '2025-03-26', capabilities: {},
        clientInfo: { name: 'gongwen-public-search', version: '1.0.0' } }, 1);
      await request('notifications/initialized');
      const keywords = `${event} ${city}`.trim();
      const response = await request('tools/call', { name: 'web_search', arguments: {
        objective: `定位${keywords}的主办方或政府发布的正式会期原始公告，区分主会与配套活动。`,
        search_queries: [`${keywords} 官方 会期`, `${keywords} 主办方 公告 日期`],
      } }, 2);
      const content = response?.content?.find(item => item.type === 'text');
      if (response?.isError || !content) return result('unavailable');
      const matches = JSON.parse(content.text).results;
      if (!Array.isArray(matches)) return result('unavailable');
      const sources = matches.filter(item => typeof item?.url === 'string' && item.url.startsWith('https://'))
        .slice(0, 6).map(item => ({ url: item.url, title: String(item.title ?? '').trim().slice(0, 180),
          excerpts: (Array.isArray(item.excerpts) ? item.excerpts : []).slice(0, 2)
            .map(excerpt => String(excerpt).slice(0, 1200)) }));
      return result(sources.length ? 'results' : 'no_results', { sources });
    } catch {
      return result('unavailable');
    }
  }
}
