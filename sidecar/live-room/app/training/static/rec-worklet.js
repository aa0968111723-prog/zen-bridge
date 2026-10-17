// AudioWorklet: forwards mono Float32 blocks to the page. Loaded from 'self' (CSP script-src 'self').
class ZenRecorder extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch && ch.length) this.port.postMessage(ch.slice(0));
    return true;
  }
}
registerProcessor("zen-recorder", ZenRecorder);
