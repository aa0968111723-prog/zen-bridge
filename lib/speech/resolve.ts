import { getEnv, type AppEnv } from '../env';
import { qwenProvider } from './qwen';
import { openaiProvider } from './openai';
import { breezeProvider } from './breeze';
import type { SpeechProvider } from './types';

export function providerName(env: AppEnv): SpeechProvider['name'] | '' {
  const selected = env.SPEECH_PROVIDER;
  if (selected === 'breeze' && env.BREEZE_ASR_URL) return 'breeze';
  if (selected === 'openai' && env.OPENAI_API_KEY) return 'openai';
  if (selected === 'qwen-live' && env.DASHSCOPE_API_KEY) return 'qwen-live';
  return '';
}
export function resolveSpeech(env = getEnv(), requested?: SpeechProvider['name']): SpeechProvider {
  const name = requested ?? providerName(env);
  if (name === 'breeze') {
    if (!env.BREEZE_ASR_URL) throw new Error('Breeze ASR 未連接。請設定可用的本機 BREEZE_ASR_URL。');
    return breezeProvider(env);
  }
  if (name === 'qwen-live') {
    if (!env.DASHSCOPE_API_KEY) throw new Error('Qwen Live 未連接。請設定 DASHSCOPE_API_KEY。');
    return qwenProvider(env);
  }
  if (name === 'openai') {
    if (!env.OPENAI_API_KEY) throw new Error('OpenAI 語音辨識未連接。請設定 OPENAI_API_KEY。');
    return openaiProvider(env);
  }
  throw new Error('語音服務尚未連接。請設定所選服務需要的連線資訊。');
}
