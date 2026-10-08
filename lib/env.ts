import { AsyncLocalStorage } from 'node:async_hooks';

export type DeployTarget = 'cloudflare' | 'zeabur';
type RawEnv = Partial<Record<
  'DASHSCOPE_API_KEY' | 'DASHSCOPE_REGION' | 'QWEN_LIVE_MODEL' | 'SPEECH_PROVIDER' | 'SPEECH_MODE' |
  'HOTWORD_LIMIT' | 'OPENAI_API_KEY' | 'OPENAI_TRANSCRIPTION_MODEL' |
  'OPENAI_TRANSLATION_MODEL' | 'DATABASE_URL' | 'DEPLOY_TARGET' | 'AUDIO_DIR' |
  'HERMES_API_URL' | 'HERMES_API_KEY' | 'HERMES_TRANSLATION_MODEL' | 'TRANSLATION_PROVIDER' |
  'BREEZE_ASR_URL' | 'BREEZE_AGENT_TOKEN' | 'PUBLIC_BASE_URL' | 'SHARE', string>>;
const context = new AsyncLocalStorage<Cloudflare.Env>();
export const QWEN_MODEL = 'qwen3.8-livetranslate-flash-realtime';

export function parseEnv(raw: RawEnv) {
  // An explicitly supplied value, including an empty string, must be valid.
  if (raw.DEPLOY_TARGET !== undefined && raw.DEPLOY_TARGET !== 'cloudflare' && raw.DEPLOY_TARGET !== 'zeabur') {
    throw new Error('DEPLOY_TARGET 必須為 cloudflare 或 zeabur。');
  }
  const DEPLOY_TARGET: DeployTarget = raw.DEPLOY_TARGET ?? (raw.DATABASE_URL ? 'zeabur' : 'cloudflare');
  if (raw.TRANSLATION_PROVIDER !== undefined && raw.TRANSLATION_PROVIDER !== 'hermes' && raw.TRANSLATION_PROVIDER !== 'openai') {
    throw new Error('TRANSLATION_PROVIDER 必須為 hermes 或 openai。');
  }
  if (raw.SPEECH_PROVIDER !== undefined && !['breeze', 'qwen-live', 'openai'].includes(raw.SPEECH_PROVIDER)) {
    throw new Error('SPEECH_PROVIDER 必須為 breeze、qwen-live 或 openai。');
  }
  if (raw.SPEECH_MODE !== undefined && raw.SPEECH_MODE !== 'stream' && raw.SPEECH_MODE !== 'chunk') {
    throw new Error('SPEECH_MODE 必須為 stream 或 chunk。');
  }
  if (raw.SHARE !== undefined && raw.SHARE !== 'off' && raw.SHARE !== 'room') {
    throw new Error('SHARE 必須為 off 或 room。');
  }
  const validateUrl = (value: string | undefined, name: string, localOnly = false) => {
    if (!value) return undefined;
    try {
      const url = new URL(value);
      const local = ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
      if (url.username || url.password || url.search || url.hash || !['http:', 'https:'].includes(url.protocol) || (localOnly && !local)) throw new Error();
      return url.toString().replace(/\/$/, '');
    } catch { throw new Error(`${name} 格式不正確。`); }
  };
  if (raw.HERMES_API_URL) {
    try {
      const url = new URL(raw.HERMES_API_URL);
      const privateNode = DEPLOY_TARGET === 'zeabur' && url.hostname.endsWith('.zeabur.internal');
      const local = url.hostname === 'localhost' || url.hostname === '127.0.0.1' || url.hostname === '[::1]';
      if (url.username || url.password || url.search || url.hash ||
        (url.protocol !== 'https:' && !(url.protocol === 'http:' && (privateNode || local)))) throw new Error();
    } catch { throw new Error('HERMES_API_URL 格式不正確。'); }
  }
  if (raw.HERMES_API_KEY && /[\r\n]/.test(raw.HERMES_API_KEY)) throw new Error('HERMES_API_KEY 格式不正確。');
  if (raw.BREEZE_AGENT_TOKEN && (raw.BREEZE_AGENT_TOKEN.length < 32 || /[\r\n]/.test(raw.BREEZE_AGENT_TOKEN))) throw new Error('BREEZE_AGENT_TOKEN 格式不正確。');
  const n = Number(raw.HOTWORD_LIMIT ?? 200);
  return {
    DASHSCOPE_API_KEY: raw.DASHSCOPE_API_KEY,
    DASHSCOPE_REGION: raw.DASHSCOPE_REGION ?? 'intl',
    QWEN_LIVE_MODEL: raw.QWEN_LIVE_MODEL || QWEN_MODEL,
    SPEECH_PROVIDER: raw.SPEECH_PROVIDER ?? 'breeze',
    SPEECH_MODE: (raw.SPEECH_MODE ?? (raw.SPEECH_PROVIDER === 'qwen-live' ? 'stream' : 'chunk')) as 'stream' | 'chunk',
    HOTWORD_LIMIT: !Number.isFinite(n) || n < 1 ? 200 : Math.min(1000, Math.floor(n)),
    OPENAI_API_KEY: raw.OPENAI_API_KEY,
    OPENAI_TRANSCRIPTION_MODEL: raw.OPENAI_TRANSCRIPTION_MODEL || 'gpt-transcribe',
    OPENAI_TRANSLATION_MODEL: raw.OPENAI_TRANSLATION_MODEL || 'gpt-4.1-mini',
    DATABASE_URL: raw.DATABASE_URL,
    DEPLOY_TARGET,
    AUDIO_DIR: raw.AUDIO_DIR,
    HERMES_API_URL: raw.HERMES_API_URL,
    HERMES_API_KEY: raw.HERMES_API_KEY,
    HERMES_TRANSLATION_MODEL: raw.HERMES_TRANSLATION_MODEL || 'hermes-agent',
    TRANSLATION_PROVIDER: raw.TRANSLATION_PROVIDER,
    BREEZE_ASR_URL: validateUrl(raw.BREEZE_ASR_URL, 'BREEZE_ASR_URL', true),
    BREEZE_AGENT_TOKEN: raw.BREEZE_AGENT_TOKEN,
    PUBLIC_BASE_URL: validateUrl(raw.PUBLIC_BASE_URL, 'PUBLIC_BASE_URL'),
    SHARE: (raw.SHARE ?? 'room') as 'off' | 'room',
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
