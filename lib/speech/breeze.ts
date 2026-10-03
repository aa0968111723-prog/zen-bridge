import type { AppEnv } from '../env';
import type { SpeechProvider } from './types';

const PREFIX = '以下是普通話的句子，請用繁體中文輸出。常見專有名詞：';

function punctuate(text: string) {
  const normalized = text.trim().replace(/\s+([，。！？；：])/g, '$1');
  return normalized && !/[。！？.!?]$/.test(normalized) ? normalized + '。' : normalized;
}

export function breezeProvider(env: AppEnv): SpeechProvider {
  return {
    name: 'breeze',
    async transcribe(input) {
      if (!env.BREEZE_ASR_URL) throw new Error('Breeze ASR 未連接。請設定可用的本機 BREEZE_ASR_URL。');
      const words = Object.keys(input.phrases).slice(0, 30);
      const prompt = PREFIX + (words.length ? words.join('、') + '。' : '無。');
      const body = new FormData();
      body.set('audio', new Blob([input.audio], { type: input.mime || 'audio/wav' }), input.filename || 'speech.wav');
      body.set('language', 'zh');
      body.set('task', 'transcribe');
      body.set('initial_prompt', prompt);
      body.set('hotwords', words.join('、'));
      let response: Response;
      try {
        response = await fetch(env.BREEZE_ASR_URL, { method: 'POST', body, signal: AbortSignal.timeout(120000) });
      } catch {
        throw new Error('Breeze ASR 無法連線。請檢查本機 sidecar 與 BREEZE_ASR_URL。');
      }
      if (!response.ok) throw new Error('Breeze ASR 回應失敗。請檢查本機 sidecar。');
      const result = await response.json() as { source?: unknown };
      if (typeof result.source !== 'string') throw new Error('Breeze ASR 回應格式不正確。');
      return [{ source: punctuate(result.source), translation: '', provider: 'breeze',
        speakerKey: input.auto ? null : input.speakerKey ?? null, offset: 0, note: '' }];
    },
  };
}
