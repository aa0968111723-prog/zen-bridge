import type {Memory} from './domain';

export type QwenLiveResult = {source: string; translation: string};

const MODEL = 'qwen3.8-livetranslate-flash-realtime';

export function qwenEndpoint(region?: string) {
  const host = region === 'cn' ? 'dashscope.aliyuncs.com' : 'dashscope-intl.aliyuncs.com';
  return `https://${host}/api-ws/v1/realtime?model=${MODEL}`;
}

export function hotwordPhrases(memories: Memory[], direction: 'zh-en' | 'en-zh') {
  const phrases: Record<string, string> = {};
  for (const memory of memories) {
    const source = (direction === 'zh-en' ? memory.zh : memory.en).trim();
    const target = (direction === 'zh-en' ? memory.en : memory.zh).trim();
    if (!source || !target || source.length > 40 || target.length > 80) continue;
    phrases[source] = target;
    if (Object.keys(phrases).length >= 200) break;
  }
  return phrases;
}

function wavToPcm16k(bytes: Uint8Array) {
  if (bytes.length < 44 || String.fromCharCode(...bytes.slice(0, 4)) !== 'RIFF') {
    throw new Error('請送出 16-bit PCM WAV。目前的 8 秒片段需為 WAV，不能是壓縮音檔。');
  }
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let offset = 12, sampleRate = 16000, channels = 1, bits = 16, dataStart = 0, dataSize = 0;
  while (offset + 8 <= bytes.length) {
    const id = String.fromCharCode(bytes[offset], bytes[offset + 1], bytes[offset + 2], bytes[offset + 3]);
    const size = view.getUint32(offset + 4, true);
    if (id === 'fmt ') {
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
  if (!dataStart || bits !== 16) throw new Error('語音片段需為 16-bit PCM WAV。');
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
    const left = Math.floor(pos);
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

export async function qwenLiveTranslate(file: Uint8Array, options: {apiKey: string; region?: string; direction: 'zh-en' | 'en-zh'; phrases: Record<string, string>}) {
  const pcm = wavToPcm16k(file);
  const target = options.direction === 'zh-en' ? 'en' : 'zh';
  const response = await fetch(qwenEndpoint(options.region), {
    headers: {Upgrade: 'websocket', Authorization: 'Bearer ' + options.apiKey},
  });
  const ws = response.webSocket;
  if (!ws) throw new Error('阿里雲同傳連線失敗，請確認新加坡節點金鑰與網路。');
  ws.accept();
  const result = await new Promise<QwenLiveResult>((resolve, reject) => {
    let source = '', translation = '', settled = false;
    const finish = (error?: Error) => {
      if (settled) return;
      settled = true;
      try { ws.close(); } catch { /* already closed */ }
      if (error) reject(error);
      else resolve({source: source.trim(), translation: translation.trim()});
    };
    const timer = setTimeout(() => finish(new Error('阿里雲同傳超時，請稍後再試。')), 50000);
    ws.addEventListener('message', event => {
      const data = typeof event.data === 'string' ? event.data : '';
      let body: {type?: string; transcript?: string; text?: string; delta?: string; error?: {message?: string}};
      try { body = JSON.parse(data); } catch { return; }
      if (body.type === 'error') { clearTimeout(timer); finish(new Error(body.error?.message || '阿里雲同傳回傳錯誤。')); return; }
      if (body.type === 'conversation.item.input_audio_transcription.completed') source = body.transcript || source;
      if (body.type === 'response.audio_transcript.done' || body.type === 'response.text.done') translation = body.transcript || body.text || translation;
      if (body.type === 'response.audio_transcript.delta' || body.type === 'response.text.delta') translation += body.delta || '';
      if (body.type === 'session.finished') { clearTimeout(timer); finish(); }
    });
    ws.addEventListener('error', () => { clearTimeout(timer); finish(new Error('阿里雲同傳連線中斷。')); });
    ws.addEventListener('close', () => { clearTimeout(timer); if (!settled) finish(); });
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
  });
  if (!result.translation && !result.source) throw new Error('阿里雲同傳沒有辨出文字，請確認麥克風與語音片段。');
  return result;
}
