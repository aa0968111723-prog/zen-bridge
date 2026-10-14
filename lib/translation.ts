import type { AppEnv } from './env';
import { hermesConfigured, translateWithHermes } from './hermes';

export type TextTranslationProvider = 'hermes' | 'openai' | 'ollama' | '';
export function textTranslationProvider(env: AppEnv): TextTranslationProvider {
  if (env.TRANSLATION_PROVIDER === 'ollama') return env.OLLAMA_URL ? 'ollama' : '';
  if (env.TRANSLATION_PROVIDER === 'hermes') return hermesConfigured(env) ? 'hermes' : '';
  if (env.TRANSLATION_PROVIDER === 'openai') return env.OPENAI_API_KEY ? 'openai' : '';
  if (hermesConfigured(env)) return 'hermes';
  return env.OPENAI_API_KEY ? 'openai' : '';
}
export function textTranslationModel(env: AppEnv): string {
  if(textTranslationProvider(env)==='ollama')return env.OLLAMA_TRANSLATION_MODEL;
  return textTranslationProvider(env) === 'hermes' ? env.HERMES_TRANSLATION_MODEL : env.OPENAI_TRANSLATION_MODEL;
}
export async function translateUtterance(instructions: string, input: string, env: AppEnv): Promise<string> {
  const provider = textTranslationProvider(env);
  if(provider==='ollama'){
    const response=await fetch(env.OLLAMA_URL+'/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},redirect:'error',signal:AbortSignal.timeout(55000),body:JSON.stringify({model:env.OLLAMA_TRANSLATION_MODEL,stream:false,think:false,messages:[{role:'system',content:instructions},{role:'user',content:input}],options:{num_thread:4,num_ctx:4096,num_predict:1800}})});
    if(!response.ok)throw new Error('本機 Ollama 翻譯失敗，請確認模型已啟動。');
    const body=await response.json() as {message?:{content?:unknown}};
    if(typeof body.message?.content!=='string'||!body.message.content.trim()||body.message.content.length>16000)throw new Error('本機模型未回傳有效譯文。');
    return body.message.content.trim();
  }
  if (provider === 'hermes') return translateWithHermes(instructions, input, env);
  if (!provider) throw new Error(env.TRANSLATION_PROVIDER === 'hermes'
    ? 'Hermes 尚未連接。請設定 HERMES_API_URL 與 HERMES_API_KEY。'
    : '文字翻譯尚未連接。請設定 Hermes 或 OPENAI_API_KEY。');
  let response: Response;
  try {
    response = await fetch('https://api.openai.com/v1/responses', {
      method: 'POST', headers: { Authorization: 'Bearer ' + env.OPENAI_API_KEY, 'Content-Type': 'application/json' },
      signal: AbortSignal.timeout(45000),
      body: JSON.stringify({ model: env.OPENAI_TRANSLATION_MODEL, instructions, input, max_output_tokens: 1800 }),
    });
  } catch { throw new Error('OpenAI 翻譯連線失敗，請檢查平台網路與 OPENAI_API_KEY。'); }
  if (!response.ok) throw new Error(response.status === 429 ? 'AI 服務忙碌或額度不足，請稍後重試。' : '翻譯服務連線失敗，請檢查服務設定。');
  let body: { output?: { content?: { type: string; text?: string }[] }[] };
  try { body = await response.json() as typeof body; }
  catch { throw new Error('OpenAI 翻譯服務回傳格式不正確，請檢查 OPENAI_TRANSLATION_MODEL。'); }
  const text = (body.output ?? []).flatMap(o => o.content ?? []).filter(c => c.type === 'output_text').map(c => c.text ?? '').join('').trim();
  if (!text) throw new Error('翻譯服務未回傳文字，請稍後重試。');
  return text;
}
