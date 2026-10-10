declare namespace Cloudflare {
  interface Env {
    DB?: D1Database;
    BUCKET?: R2Bucket;
    OPENAI_API_KEY?: string;
    OPENAI_TRANSLATION_MODEL?: string;
    OPENAI_TRANSCRIPTION_MODEL?: string;
    DASHSCOPE_API_KEY?: string;
    DASHSCOPE_REGION?: string;
    QWEN_LIVE_MODEL?: string;
    SPEECH_PROVIDER?: string;
    SPEECH_MODE?: string;
    BREEZE_ASR_URL?: string;
    BREEZE_AGENT_TOKEN?: string;
    PUBLIC_BASE_URL?: string;
    SHARE?: string;
    HOTWORD_LIMIT?: string;
    DATABASE_URL?: string;
    DEPLOY_TARGET?: string;
    AUDIO_DIR?: string;
    HERMES_API_URL?: string;
    HERMES_API_KEY?: string;
    HERMES_TRANSLATION_MODEL?: string;
    TRANSLATION_PROVIDER?: string;
    OLLAMA_URL?: string;
    OLLAMA_TRANSLATION_MODEL?: string;
  }
}
