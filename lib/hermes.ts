import { z } from 'zod';
import { getEnv, type AppEnv } from './env';

export function hermesConfigured(env: AppEnv): boolean {
  return !!(env.HERMES_API_URL && env.HERMES_API_KEY);
}
function apiUrl(env: AppEnv, path: string): string {
  if (!hermesConfigured(env)) throw new Error('Hermes 尚未連接。請設定 HERMES_API_URL 與 HERMES_API_KEY。');
  const base = env.HERMES_API_URL!.replace(/\/+$/, '');
  return base + (base.endsWith('/v1') ? '' : '/v1') + path;
}
async function requestHermes(path: string, env: AppEnv, body?: unknown): Promise<Response> {
  const url = apiUrl(env, path);
  let response: Response;
  try {
    response = await fetch(url, {
      method: body === undefined ? 'GET' : 'POST',
      headers: { Authorization: 'Bearer ' + env.HERMES_API_KEY, Accept: 'application/json',
        ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
      body: body === undefined ? undefined : JSON.stringify(body),
      redirect: 'error',
      signal: AbortSignal.timeout(body === undefined ? 10000 : 45000),
    });
  } catch { throw new Error('此平台無法連到 Hermes。請檢查 HERMES_API_URL 與服務網路。'); }
  if (!response.ok) {
    if (response.status === 401 || response.status === 403) throw new Error('Hermes 驗證失敗，請檢查 HERMES_API_KEY 是否對應 API_SERVER_KEY。');
    if (response.status === 429) throw new Error('Hermes 目前忙碌或模型額度不足，請稍後重試。');
    throw new Error('Hermes 服務無法完成請求，請檢查 API 端點與 Hermes 的文字模型設定。');
  }
  return response;
}
async function jsonBody(response: Response): Promise<unknown> {
  try { return await response.json(); }
  catch { throw new Error('Hermes 回傳格式不正確，請檢查 HERMES_API_URL。'); }
}
const models = z.object({ object: z.literal('list'), data: z.array(z.object({ id: z.string().min(1).max(160) })).min(1) });
const completion = z.object({ choices: z.array(z.object({
  message: z.object({ role: z.literal('assistant'), content: z.string() }),
  finish_reason: z.string().nullable().optional(),
})).min(1) });

export async function checkHermesConnection(env = getEnv()) {
  const result = models.safeParse(await jsonBody(await requestHermes('/models', env)));
  if (!result.success) throw new Error('Hermes 未回傳可用的模型，請檢查服務設定。');
  return { status: 'connected' as const, model: result.data.data.find(m => m.id === env.HERMES_TRANSLATION_MODEL)?.id ?? result.data.data[0].id };
}

export async function translateWithHermes(instructions: string, input: string, env: AppEnv): Promise<string> {
  // This is a server-to-server text call. The current Hermes API has no audio endpoint.
  // Hermes executes its configured tools server-side; tool_choice is not a security
  // control on all Hermes versions. Use an isolated translation profile for a public site.
  const result = completion.safeParse(await jsonBody(await requestHermes('/chat/completions', env, {
    model: env.HERMES_TRANSLATION_MODEL,
    messages: [{ role: 'system', content: instructions }, { role: 'user', content: input }],
    stream: false,
  })));
  if (!result.success) throw new Error('Hermes 翻譯回傳格式不正確，請檢查服務設定。');
  const choice = result.data.choices[0], text = choice.message.content.trim();
  if (choice.finish_reason && choice.finish_reason !== 'stop') throw new Error('Hermes 翻譯尚未完整完成，請稍後重試。');
  if (!text || text.length > 16000) throw new Error('Hermes 未回傳完整譯文，請稍後重試。');
  return text;
}
