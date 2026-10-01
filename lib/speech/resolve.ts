import { getEnv, type AppEnv } from '../env';
import { qwenProvider } from './qwen';
import { openaiProvider } from './openai';
import type { SpeechProvider } from './types';

export function providerName(env: AppEnv): SpeechProvider['name'] | '' {
  const selected = env.SPEECH_PROVIDER;
  if (selected !== undefined && selected !== 'qwen' && selected !== 'openai') {
    throw new Error('不支援的 SPEECH_PROVIDER。目前可用：qwen、openai。');
  }
  if (selected === 'openai' && env.OPENAI_API_KEY) return 'openai';
  if ((selected === 'qwen' || selected === undefined) && env.DASHSCOPE_API_KEY) return 'qwen-live';
  if (env.OPENAI_API_KEY) return 'openai';
  return '';
}
export function resolveSpeech(env = getEnv()): SpeechProvider {
  const name = providerName(env);
  if (name === 'qwen-live') return qwenProvider(env);
  if (name === 'openai') return openaiProvider(env);
  throw new Error('語音服務尚未連接。請設定 DASHSCOPE_API_KEY 或 OPENAI_API_KEY。');
}

// breeze-25：只能透過外部 HTTP 服務，不能把模型放進 Workers 或 Zeabur 映像。此處不實作。
