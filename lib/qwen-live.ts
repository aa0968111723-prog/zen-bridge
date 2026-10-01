import { QWEN_MODEL, type DeployTarget } from './env';

export type QwenLiveResult = {source: string; translation: string};

export type LiveSocket = {
  send(data: string): void;
  close(): void;
  addEventListener(type: string, listener: (event: { data?: unknown; code?: number }) => void): void;
};

export function qwenEndpoint(region?: string, model = QWEN_MODEL) {
  const host = region === 'cn' ? 'dashscope.aliyuncs.com' : 'dashscope-intl.aliyuncs.com';
  return `https://${host}/api-ws/v1/realtime?model=${encodeURIComponent(model)}`;
}

function wavToPcm16k(bytes: Uint8Array) {
  if (bytes.length < 44 || String.fromCharCode(...bytes.slice(0, 4)) !== 'RIFF' || String.fromCharCode(...bytes.slice(8, 12)) !== 'WAVE') {
    throw new Error('請送出 16-bit PCM WAV，不能是壓縮音檔。');
  }
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let offset = 12, sampleRate = 0, channels = 0, bits = 0, format = 0, dataStart = 0, dataSize = 0;
  while (offset + 8 <= bytes.length) {
    const id = String.fromCharCode(bytes[offset], bytes[offset + 1], bytes[offset + 2], bytes[offset + 3]);
    const size = view.getUint32(offset + 4, true);
    if (offset + 8 + size > bytes.length) throw new Error('WAV 音檔不完整。');
    if (id === 'fmt ') {
      if (size < 16) throw new Error('WAV 音檔格式不正確。');
      format = view.getUint16(offset + 8, true);
      channels = view.getUint16(offset + 10, true);
      sampleRate = view.getUint32(offset + 12, true);
      bits = view.getUint16(offset + 22, true);
    } else if (id === 'data') {
      dataStart = offset + 8;
      dataSize = size;
      break;
    }
    offset += 8 + size + (size % 2);
  }
  if (!dataStart || !dataSize || format !== 1 || bits !== 16 || channels < 1 || sampleRate < 1 || dataSize % (2 * channels)) throw new Error('語音片段需為 16-bit PCM WAV。');
  const samples = Math.floor(dataSize / 2 / channels);
  const mono = new Int16Array(samples);
  for (let i = 0; i < samples; i++) {
    let sum = 0;
    for (let c = 0; c < channels; c++) sum += view.getInt16(dataStart + (i * channels + c) * 2, true);
    mono[i] = Math.max(-32768, Math.min(32767, Math.round(sum / channels)));
  }
  if (sampleRate === 16000) return mono;
  const outLen = Math.max(1, Math.round(mono.length * 16000 / sampleRate));
  const out = new Int16Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const pos = i * sampleRate / 16000;
    const left = Math.min(mono.length - 1, Math.floor(pos));
    const right = Math.min(mono.length - 1, left + 1);
    const mix = pos - left;
    out[i] = Math.round(mono[left] * (1 - mix) + mono[right] * mix);
  }
  return out;
}

function pcmBase64(samples: Int16Array, start: number, end: number) {
  const slice = samples.subarray(start, end);
  const bytes = new Uint8Array(slice.buffer, slice.byteOffset, slice.byteLength);
  let binary = '';
  const step = 0x8000;
  for (let i = 0; i < bytes.length; i += step) binary += String.fromCharCode(...bytes.subarray(i, i + step));
  return btoa(binary);
}

export async function qwenLiveTranslate(file: Uint8Array, options: {apiKey: string; region?: string; model?: string; platform: DeployTarget; direction: 'zh-en' | 'en-zh'; phrases: Record<string, string>}) {
  const pcm = wavToPcm16k(file);
  const target = options.direction === 'zh-en' ? 'en' : 'zh';
  const platform = options.platform === 'zeabur' ? 'Zeabur' : 'Cloudflare';
  const connectionError = () => new Error(`${platform} 無法連線至阿里雲。請檢查平台網路與 DASHSCOPE_API_KEY。`);
  let ws: LiveSocket;
  try {
    const endpoint = qwenEndpoint(options.region, options.model);
    if (options.platform === 'zeabur') {
      const { openNodeSocket } = await import('@/lib/platform/node');
      ws = await openNodeSocket(endpoint, options.apiKey);
    } else {
      const response = await fetch(endpoint, { headers: { Upgrade: 'websocket', Authorization: 'Bearer ' + options.apiKey }, signal: AbortSignal.timeout(15000) });
      if (!response.webSocket) throw connectionError();
      response.webSocket.accept();
      ws = response.webSocket;
    }
  } catch { throw connectionError(); }
  const result = await new Promise<QwenLiveResult>((resolve, reject) => {
    let source = '', translation = '', settled = false;
    const finish = (error?: Error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try { ws.close(); } catch { /* already closed */ }
      if (error) reject(error);
      else resolve({source: source.trim(), translation: translation.trim()});
    };
    const timer = setTimeout(() => finish(new Error('阿里雲同傳超時，請稍後再試。')), 50000);
    ws.addEventListener('message', event => {
      const data = typeof event.data === 'string' ? event.data : '';
      let body: {type?: string; transcript?: string; text?: string; delta?: string; error?: {message?: string}};
      try { body = JSON.parse(data); } catch { return; }
      if (body.type === 'error') { finish(new Error('阿里雲同傳回傳錯誤。請檢查 DASHSCOPE_API_KEY、QWEN_LIVE_MODEL 與 DASHSCOPE_REGION。')); return; }
      if (body.type === 'conversation.item.input_audio_transcription.completed') source = body.transcript || source;
      if (body.type === 'response.audio_transcript.done' || body.type === 'response.text.done') translation = body.transcript || body.text || translation;
      if (body.type === 'response.audio_transcript.delta' || body.type === 'response.text.delta') translation += body.delta || '';
      if (body.type === 'session.finished') { clearTimeout(timer); finish(); }
    });
    ws.addEventListener('error', () => finish(connectionError()));
    ws.addEventListener('close', event => { if (!settled) finish(event.code === 1000 && (source || translation) ? undefined : connectionError()); });
    try {
    ws.send(JSON.stringify({
      type: 'session.update',
      session: {
        output_modalities: ['text'],
        translation: {language: target, corpus: {phrases: options.phrases}},
      },
    }));
    const frame = 1600;
    for (let i = 0; i < pcm.length; i += frame) {
      ws.send(JSON.stringify({type: 'input_audio_buffer.append', audio: pcmBase64(pcm, i, Math.min(pcm.length, i + frame))}));
    }
    ws.send(JSON.stringify({type: 'session.finish'}));
    } catch { finish(connectionError()); }
  });
  return result;
}
