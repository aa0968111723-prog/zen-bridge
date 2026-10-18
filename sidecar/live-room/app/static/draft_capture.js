// round4 #5: host-side draft stream. Mic -> 16 kHz mono PCM16, 100 ms packets -> /ws/draft.
// Drafts are screen-only on the listener side; the real 6 s slices still go to /api/push.
// grok/fast-draft-asr: with ``finalize`` the server (BREEZE_ENDPOINT_FINAL=1) cuts each sentence at
// a pause and lets Breeze finalise it (~2-4 s); the host then stops uploading 6 s slices while
// ``onFinalize(true)`` holds, and falls back to them as soon as the stream ends.
export const DRAFT_RATE = 16000;
export const PACKET_SAMPLES = 1600;          // 100 ms
export const MAX_BUFFERED = 64 * 1024;       // skip packets instead of queueing on a slow link

export function toPcm16(float32, inRate, outRate = DRAFT_RATE) {
  const input = float32 || new Float32Array(0);
  if (!inRate || inRate === outRate) {
    const out = new Int16Array(input.length);
    for (let i = 0; i < input.length; i += 1) {
      const v = Math.max(-1, Math.min(1, input[i]));
      out[i] = v < 0 ? Math.round(v * 0x8000) : Math.round(v * 0x7fff);
    }
    return out;
  }
  const ratio = inRate / outRate;
  const n = Math.floor(input.length / ratio);
  const out = new Int16Array(n);
  for (let i = 0; i < n; i += 1) {
    const pos = i * ratio;
    const a = Math.floor(pos);
    const b = Math.min(input.length - 1, a + 1);
    const v = Math.max(-1, Math.min(1, input[a] + (input[b] - input[a]) * (pos - a)));
    out[i] = v < 0 ? Math.round(v * 0x8000) : Math.round(v * 0x7fff);
  }
  return out;
}

export function draftUrl(loc, roomId, sessionId) {
  const proto = loc.protocol === "https:" ? "wss" : "ws";
  return proto + "://" + loc.host + "/ws/draft?room_id=" + encodeURIComponent(roomId) +
    "&session_id=" + encodeURIComponent(sessionId);
}

// A tiny packetizer, separated so tests can drive it without audio hardware.
export function createPacketizer(send, inRate) {
  let pending = [];
  let count = 0;
  return {
    push(float32) {
      const pcm = toPcm16(float32, inRate);
      pending.push(pcm);
      count += pcm.length;
      while (count >= PACKET_SAMPLES) {
        const packet = new Int16Array(PACKET_SAMPLES);
        let filled = 0;
        while (filled < PACKET_SAMPLES) {
          const head = pending[0];
          const take = Math.min(head.length, PACKET_SAMPLES - filled);
          packet.set(head.subarray(0, take), filled);
          filled += take;
          if (take === head.length) pending.shift(); else pending[0] = head.subarray(take);
        }
        count -= PACKET_SAMPLES;
        send(packet.buffer);
      }
    },
    reset() { pending = []; count = 0; },
  };
}

export const FLUSH_WAIT_MS = 1500;

export function draftHello(token, { finalize = false, t0Ms = 0 } = {}) {
  const hello = { token };
  if (finalize) {
    hello.finalize = true;
    hello.t0_ms = Math.max(0, Math.round(Number(t0Ms) || 0));
  }
  return JSON.stringify(hello);
}

// Control messages other than draft_on/draft_off. Returns true when the message was handled.
export function handleControl(msg, { onFinalSeq = () => {}, onFlushed = () => {} } = {}) {
  if (!msg || typeof msg !== "object") return false;
  if (msg.type === "final_seq" && Number.isInteger(msg.seq)) { onFinalSeq(msg); return true; }
  if (msg.type === "final_dropped" && Number.isInteger(msg.seq)) { onFinalSeq(msg); return true; }
  if (msg.type === "flushed") { onFlushed(msg); return true; }
  return false;
}

const WORKLET = `class P extends AudioWorkletProcessor{process(i){const c=i[0];if(c&&c[0])this.port.postMessage(c[0].slice(0));return true}};registerProcessor("zen-draft",P);`;

export async function startDraftStream({ stream, roomId, sessionId, token, loc = location,
  AudioContextCtor = globalThis.AudioContext, WebSocketCtor = globalThis.WebSocket, onState = () => {},
  finalize = false, t0Ms = 0, onFinalize = () => {}, onFinalSeq = () => {}, flushWaitMs = FLUSH_WAIT_MS }) {
  const ws = new WebSocketCtor(draftUrl(loc, roomId, sessionId));
  ws.binaryType = "arraybuffer";
  let ctx = null;
  let stopped = false;
  let node = null;
  let src = null;
  let finalizing = false;
  let flushedResolve = null;
  const own = stream.clone();
  const teardown = () => {
    try { node && node.disconnect(); } catch { /* gone */ }
    try { src && src.disconnect(); } catch { /* gone */ }
    try { ctx && ctx.close(); } catch { /* gone */ }
    for (const t of own.getTracks()) t.stop();
  };
  const stop = async () => {
    if (stopped) return;
    stopped = true;
    teardown();
    if (finalizing && ws.readyState === 1) {
      // let the server finalise the sentence still open before the session ends
      await new Promise((resolve) => {
        const timer = setTimeout(resolve, flushWaitMs);
        flushedResolve = () => { clearTimeout(timer); resolve(); };
        try { ws.send(JSON.stringify({ type: "flush" })); } catch { flushedResolve(); }
      });
    }
    try { ws.close(); } catch { /* gone */ }
    if (finalizing) { finalizing = false; onFinalize(false); }
    onState("off");
  };
  ws.onmessage = async (ev) => {
    let msg = null;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (handleControl(msg, { onFinalSeq, onFlushed: () => flushedResolve && flushedResolve() })) return;
    if (msg.type === "draft_off") { stop(); return; }
    if (msg.type !== "draft_on" || stopped) return;
    if (msg.finalize === true && finalize) { finalizing = true; onFinalize(true); }
    try {
      try { ctx = new AudioContextCtor({ sampleRate: DRAFT_RATE }); } catch { ctx = new AudioContextCtor(); }
      const pk = createPacketizer((buf) => {
        if (ws.readyState === 1 && ws.bufferedAmount < MAX_BUFFERED) ws.send(buf);
      }, ctx.sampleRate);
      src = ctx.createMediaStreamSource(own);
      const blobUrl = URL.createObjectURL(new Blob([WORKLET], { type: "text/javascript" }));
      await ctx.audioWorklet.addModule(blobUrl);
      URL.revokeObjectURL(blobUrl);
      node = new AudioWorkletNode(ctx, "zen-draft");
      node.port.onmessage = (e) => { if (!stopped) pk.push(e.data); };
      src.connect(node);
      onState("on");
    } catch { stop(); }
  };
  ws.onopen = () => ws.send(draftHello(token, { finalize, t0Ms }));
  ws.onclose = () => {
    if (flushedResolve) flushedResolve();
    if (finalizing) { finalizing = false; onFinalize(false); }   // fall back to 6 s uploads at once
    if (!stopped) stop();
  };
  ws.onerror = () => {};
  return { stop };
}
