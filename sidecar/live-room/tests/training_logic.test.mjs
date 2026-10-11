import assert from "node:assert/strict";
import {encodeWav, fmtMinutes, level, meterPct, precheck, resample} from "../app/training/static/training_logic.js";

const sine = (n, rate, amp = 0.3) => Float32Array.from({length: n}, (_, i) => amp * Math.sin(2 * Math.PI * 220 * i / rate));
const r = resample(sine(48000, 48000), 48000, 16000);
assert.equal(r.length, 16000);
assert.ok(Math.abs(level(r).peak - 0.3) < 0.02);
const wav = encodeWav(r);
assert.equal(wav.length, 44 + 32000);
const v = new DataView(wav.buffer);
assert.equal(String.fromCharCode(...wav.slice(0, 4)), "RIFF");
assert.equal(v.getUint16(22, true), 1);          // mono
assert.equal(v.getUint32(24, true), 16000);      // 16 kHz
assert.equal(v.getUint16(34, true), 16);         // 16-bit
assert.deepEqual(precheck(sine(16000 * 2, 16000)), []);
assert.match(precheck(sine(8000, 16000)).join(), /太短/);
assert.match(precheck(sine(16000 * 2, 16000, 1.2)).join(), /削波/);
assert.match(precheck(sine(16000 * 2, 16000, 0.001)).join(), /太小/);
assert.equal(level(new Float32Array([1, -1, 0.5])).clipped, 2);
assert.equal(meterPct(-60), 0); assert.equal(meterPct(0), 100); assert.equal(meterPct(-30), 50);
assert.equal(fmtMinutes(1.5), "1 分 30 秒"); assert.equal(fmtMinutes(undefined), "—");
console.log("training_logic ok");
