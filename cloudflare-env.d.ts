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
    HOTWORD_LIMIT?: string;
    DATABASE_URL?: string;
    DEPLOY_TARGET?: string;
    AUDIO_DIR?: string;
  }
}
