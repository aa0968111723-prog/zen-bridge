// Pure helpers for the training studio (tested in tests/training_logic.test.mjs). No DOM.
export const TARGET_RATE = 16000;

/** Level of one Float32 block: {rms, peak, dbfs, clipped} (clipped = samples at |x| >= 0.98). */
export function level(block) {
  let sq = 0, peak = 0, clipped = 0;
  for (let i = 0; i < block.length; i++) {
    const a = Math.abs(block[i]);
    sq += block[i] * block[i];
    if (a > peak) peak = a;
    if (a >= 0.98) clipped++;
  }
  const rms = block.length ? Math.sqrt(sq / block.length) : 0;
  return {rms, peak, dbfs: rms > 0 ? 20 * Math.log10(rms) : -120, clipped};
}

/** Linear resample with a box pre-filter (good enough for speech at 16 kHz). */
export function resample(input, fromRate, toRate = TARGET_RATE) {
  if (fromRate === toRate) return Float32Array.from(input);
  const ratio = fromRate / toRate;
  const n = Math.floor(input.length / ratio);
  const out = new Float32Array(n);
  const win = Math.max(1, Math.floor(ratio));
  for (let i = 0; i < n; i++) {
    const pos = i * ratio;
    const i0 = Math.floor(pos);
    let acc = 0, cnt = 0;
    for (let k = 0; k < win && i0 + k < input.length; k++) { acc += input[i0 + k]; cnt++; }
    out[i] = cnt ? acc / cnt : 0;
  }
  return out;
}

/** Float32 [-1,1] mono -> 16-bit PCM WAV bytes. */
export function encodeWav(samples, rate = TARGET_RATE) {
  const buf = new ArrayBuffer(44 + samples.length * 2);
  const v = new DataView(buf);
  const str = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  str(0, "RIFF"); v.setUint32(4, 36 + samples.length * 2, true); str(8, "WAVE");
  str(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  str(36, "data"); v.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Uint8Array(buf);
}

/** Client-side pre-check before upload (server re-checks): returns Chinese problem strings. */
export function precheck(samples, rate = TARGET_RATE) {
  const out = [];
  const secs = samples.length / rate;
  if (secs < 1) out.push("太短（少於 1 秒）");
  if (secs > 30) out.push("太長（超過 30 秒）");
  const lv = level(samples);
  if (lv.clipped / Math.max(1, samples.length) > 0.001) out.push("音量太大，有削波破音");
  if (lv.dbfs < -40) out.push("音量太小");
  return out;
}

export function fmtMinutes(m) {
  if (!(m >= 0)) return "—";
  const s = Math.round(m * 60);
  return `${Math.floor(s / 60)} 分 ${String(s % 60).padStart(2, "0")} 秒`;
}

/** Meter width 0..100 from dBFS (-60 .. 0). */
export function meterPct(dbfs) {
  return Math.max(0, Math.min(100, Math.round((dbfs + 60) / 60 * 100)));
}

export const SOURCE_ZH = {club: "社團用語", glossary: "詞表", dharma: "開示常用句", mixed: "中英夾雜", custom: "自訂"};
export const STATUS_ZH = {unverified: "未校對", needs_review: "需人工處理", approved: "已核准", rejected: "已退回"};

// 課後審核的鍵盤對應（純函式，tests/training_logic.test.mjs 測）。editing = 正在改文字。
export function reviewKey(key, {editing = false, ctrl = false} = {}) {
  if (ctrl) return null;
  if (editing) return key === "Escape" ? "cancel" : key === "Enter" ? "save" : null;
  return ({j: "next", J: "next", ArrowDown: "next", k: "prev", K: "prev", ArrowUp: "prev", " ": "play",
    a: "approve", A: "approve", Enter: "approve", e: "edit", E: "edit", x: "exclude", X: "exclude",
    i: "include", I: "include", r: "reject", R: "reject"})[key] || null;
}

export const FLAG_ZH = {low_speaker: "聲紋不太像", speaker_unknown: "太短無法比對聲紋", clipping: "破音", noisy: "雜音大",
  mostly_silence: "大多靜音", too_quiet: "太小聲", short_text: "字太少", odd_rate: "語速異常", repeat: "重複字（疑似幻覺）",
  draft_disagree: "草稿和定稿差很多", near_question: "接近提問時段", question: "提問中"};

export function fmtHours(s) { return (s / 3600).toFixed(2) + " 小時"; }
