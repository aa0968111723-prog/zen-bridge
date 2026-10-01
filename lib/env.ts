import { AsyncLocalStorage } from 'node:async_hooks';

export type DeployTarget = 'cloudflare' | 'zeabur';
type RawEnv = Partial<Record<
  'DASHSCOPE_API_KEY' | 'DASHSCOPE_REGION' | 'QWEN_LIVE_MODEL' | 'SPEECH_PROVIDER' |
  'HOTWORD_LIMIT' | 'OPENAI_API_KEY' | 'OPENAI_TRANSCRIPTION_MODEL' |
  'OPENAI_TRANSLATION_MODEL' | 'DATABASE_URL' | 'DEPLOY_TARGET' | 'AUDIO_DIR', string>>;
const context = new AsyncLocalStorage<Cloudflare.Env>();
export const QWEN_MODEL = 'qwen3.8-livetranslate-flash-realtime';

export function parseEnv(raw: RawEnv) {
  // An explicitly supplied value, including an empty string, must be valid.
  if (raw.DEPLOY_TARGET !== undefined && raw.DEPLOY_TARGET !== 'cloudflare' && raw.DEPLOY_TARGET !== 'zeabur') {
    throw new Error('DEPLOY_TARGET 必須為 cloudflare 或 zeabur。');
  }
  const DEPLOY_TARGET: DeployTarget = raw.DEPLOY_TARGET ?? (raw.DATABASE_URL ? 'zeabur' : 'cloudflare');
  const n = Number(raw.HOTWORD_LIMIT ?? 200);
  return {
    DASHSCOPE_API_KEY: raw.DASHSCOPE_API_KEY,
    DASHSCOPE_REGION: raw.DASHSCOPE_REGION ?? 'intl',
    QWEN_LIVE_MODEL: raw.QWEN_LIVE_MODEL || QWEN_MODEL,
    SPEECH_PROVIDER: raw.SPEECH_PROVIDER,
    HOTWORD_LIMIT: !Number.isFinite(n) || n < 1 ? 200 : Math.min(1000, Math.floor(n)),
    OPENAI_API_KEY: raw.OPENAI_API_KEY,
    OPENAI_TRANSCRIPTION_MODEL: raw.OPENAI_TRANSCRIPTION_MODEL || 'gpt-transcribe',
    OPENAI_TRANSLATION_MODEL: raw.OPENAI_TRANSLATION_MODEL || 'gpt-4.1-mini',
    DATABASE_URL: raw.DATABASE_URL,
    DEPLOY_TARGET,
    AUDIO_DIR: raw.AUDIO_DIR,
  };
}
export type AppEnv = ReturnType<typeof parseEnv>;
export function getEnv(): AppEnv {
  // Worker bindings are request-scoped; never copy secrets into process.env.
  return parseEnv(context.getStore() ?? (process.env as RawEnv));
}
export function initializeNodeRuntime(requestedTarget?: DeployTarget): AppEnv {
  const raw = process.env as RawEnv;
  // Validate before applying the command's default: explicit invalid values
  // must still fail, and an explicit Cloudflare target must not be overridden.
  const config = parseEnv(raw);
  if (requestedTarget && raw.DEPLOY_TARGET !== undefined && config.DEPLOY_TARGET !== requestedTarget) {
    throw new Error('DEPLOY_TARGET 與啟動命令不一致。start:zeabur 必須使用 zeabur。');
  }
  if (requestedTarget && raw.DEPLOY_TARGET === undefined) {
    // The build shell's environment does not persist into the deployed process.
    // A named runtime entry point supplies this non-secret default for the
    // entire Node process, including subsequent route and database reads.
    process.env.DEPLOY_TARGET = requestedTarget;
  }
  return getEnv();
}
export function getBindings(): Cloudflare.Env {
  return context.getStore() ?? {};
}
export function runWithEnv<T>(bindings: Cloudflare.Env, run: () => T): T {
  parseEnv(bindings);
  return context.run(bindings, run);
}
