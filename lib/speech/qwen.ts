import type { AppEnv } from '../env';
import { qwenLiveTranslate } from '../qwen-live';
import type { SpeechProvider } from './types';
export function qwenProvider(env: AppEnv): SpeechProvider {
  return {
    name: 'qwen-live',
    async transcribe(input) {
      const result = await qwenLiveTranslate(input.audio, {
        apiKey: env.DASHSCOPE_API_KEY!, region: env.DASHSCOPE_REGION,
        model: env.QWEN_LIVE_MODEL, platform: env.DEPLOY_TARGET,
        direction: input.direction, phrases: input.phrases,
      });
      return [{ ...result, provider: 'qwen-live', speakerKey: input.auto ? null : input.speakerKey ?? null,
        offset: 0, note: input.auto ? '不套用講者聲紋' : '' }];
    },
  };
}
