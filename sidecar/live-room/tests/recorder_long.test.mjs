// R1–R4 and B-c6. node:test mock.timers (Node 20+) drives the 6s slice clock.
// Upload delays are mocked setTimeout calls, so 1000 segments do not take 100 minutes.
// If mock.timers is missing, a manual clock covers the same setTimeout/setInterval/Date surface.

import assert from "node:assert/strict";
import { mock } from "node:test";
import { createCaptureController } from "../app/static/recorder_machine.js";

const clock = installClock();

function installClock() {
  if (mock.timers && typeof mock.timers.enable === "function") {
    mock.timers.enable({ apis: ["setInterval", "setTimeout", "Date"] });
    return {
      tick(ms) { mock.timers.tick(ms); },
      reset() { mock.timers.reset(); },
    };
  }
  const real = {
    setTimeout: globalThis.setTimeout,
    clearTimeout: globalThis.clearTimeout,
    setInterval: globalThis.setInterval,
    clearInterval: globalThis.clearInterval,
    Date: globalThis.Date,
  };
  let now = 0;
  let nextId = 1;
  const timers = [];
  function arm(fn, ms, interval) {
    const id = nextId;
    nextId += 1;
    timers.push({ id, fn, at: now + Math.max(0, Number(ms) || 0), interval, ms: Math.max(0, Number(ms) || 0) });
    return id;
  }
  function clear(id) {
    const index = timers.findIndex((item) => item.id === id);
    if (index >= 0) timers.splice(index, 1);
  }
  globalThis.setTimeout = (fn, ms) => arm(fn, ms, false);
  globalThis.setInterval = (fn, ms) => arm(fn, ms, true);
  globalThis.clearTimeout = clear;
  globalThis.clearInterval = clear;
  class FakeDate extends real.Date {
    constructor(...args) {
      if (args.length === 0) super(now);
      else super(...args);
    }
    static now() { return now; }
  }
  globalThis.Date = FakeDate;
  return {
    tick(ms) {
      const end = now + ms;
      let guard = 0;
      while (guard < 100000) {
        guard += 1;
        timers.sort((a, b) => a.at - b.at || a.id - b.id);
        const next = timers[0];
        if (!next || next.at > end) break;
        now = next.at;
        if (next.interval) next.at = now + next.ms;
        else timers.splice(timers.indexOf(next), 1);
        next.fn();
      }
      now = end;
    },
    reset() {
      globalThis.setTimeout = real.setTimeout;
      globalThis.clearTimeout = real.clearTimeout;
      globalThis.setInterval = real.setInterval;
      globalThis.clearInterval = real.clearInterval;
      globalThis.Date = real.Date;
      timers.length = 0;
    },
  };
}

function fakeStream() {
  const tracks = [{ stop() {}, addEventListener() {} }];
  return { getTracks: () => tracks };
}

function createRecorder() {
  const listeners = {};
  return {
    state: "inactive",
    addEventListener(name, fn) { listeners[name] = fn; },
    start() { this.state = "recording"; },
    stop() {
      this.state = "inactive";
      listeners.dataavailable?.({ data: { size: 1200 } });
    },
  };
}

async function flush() {
  for (let i = 0; i < 8; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

function controller({ delayMs, phases, collected, concurrent }) {
  return createCaptureController({
    periodMs: 6000,
    maxInflight: 2,
    openMic: async () => fakeStream(),
    createRecorder,
    newId: () => "sess",
    roomId: () => "class",
    onPhase(next) { phases?.push(next); },
    upload(meta) {
      concurrent.current += 1;
      concurrent.max = Math.max(concurrent.max, concurrent.current);
      collected.push({ ...meta });
      return new Promise((resolve) => {
        setTimeout(() => {
          concurrent.current -= 1;
          resolve({ ok: true });
        }, delayMs);
      });
    },
  });
}

async function settle(ctl) {
  if (ctl.state !== "recording" && ctl.state !== "waiting" && ctl.state !== "preparing" && ctl.state !== "draining") {
    return;
  }
  const stopping = ctl.stop();
  for (let i = 0; i < 40 && ctl.state !== "idle"; i += 1) {
    clock.tick(3000);
    await flush();
  }
  await stopping;
}

async function testNeverWaits() {
  // R1 / B-c6. A 2s upload inside a 6s slice never hits maxInflight, so there is no backpressure gap.
  const phases = [];
  const collected = [];
  const concurrent = { current: 0, max: 0 };
  const ctl = controller({ delayMs: 2000, phases, collected, concurrent });
  await ctl.start();
  await flush();
  assert.equal(ctl.state, "recording");
  for (let i = 0; i < 1000; i += 1) {
    clock.tick(6000);
    await flush();
  }
  assert.equal(ctl.gaps.length, 0);
  assert.ok(concurrent.max <= 2, `concurrent uploads ${concurrent.max}`);
  assert.ok(concurrent.max >= 1);
  const full = collected.filter((meta) => meta.seq <= 1000);
  assert.equal(full.length, 1000);
  for (let i = 0; i < full.length; i += 1) {
    assert.equal(full[i].seq, i + 1);
    const duration = full[i].t1_ms - full[i].t0_ms;
    assert.ok(Math.abs(duration - 6000) <= 50, `seg ${full[i].seq} duration ${duration}`);
    if (i > 0) assert.ok(full[i].t0_ms >= full[i - 1].t1_ms);
  }
  await settle(ctl);
  const seqs = collected.map((meta) => meta.seq);
  assert.equal(new Set(seqs).size, seqs.length);
  assert.equal(ctl.state, "idle");
  assert.equal(ctl.inflight, 0);
}

async function testCoupledUploadWaits() {
  // R2. A 42s upload (ASR and English coupled on main) fills maxInflight and the recorder waits.
  const phases = [];
  const collected = [];
  const concurrent = { current: 0, max: 0 };
  const ctl = controller({ delayMs: 42000, phases, collected, concurrent });
  await ctl.start();
  await flush();
  for (let i = 0; i < 20 && ctl.gaps.length === 0; i += 1) {
    clock.tick(6000);
    await flush();
  }
  assert.ok(ctl.gaps.length > 0);
  assert.equal(ctl.gaps[0].reason, "backpressure");
  assert.ok(concurrent.max <= 2, `concurrent uploads ${concurrent.max}`);
  await settle(ctl);
  assert.equal(ctl.inflight, 0);
}

async function testStopFlushesTail() {
  // R3. stop() uploads the in-progress slice before it resolves, then the controller is idle.
  const phases = [];
  const collected = [];
  const concurrent = { current: 0, max: 0 };
  const ctl = controller({ delayMs: 2000, phases, collected, concurrent });
  await ctl.start();
  await flush();
  clock.tick(1000);
  await flush();
  assert.equal(collected.length, 0, "the open slice is not uploaded until stop");
  const stopping = ctl.stop();
  await flush();
  assert.equal(collected.length, 1);
  assert.equal(collected[0].seq, 1);
  assert.equal(ctl.state, "draining");
  for (let i = 0; i < 10 && ctl.state !== "idle"; i += 1) {
    clock.tick(1000);
    await flush();
  }
  await stopping;
  assert.equal(ctl.state, "idle");
  assert.equal(ctl.inflight, 0);
}

async function testStopDuringWaiting() {
  // R4. Stopping while the recorder is paused for backpressure still drains, once per seq.
  const phases = [];
  const collected = [];
  const concurrent = { current: 0, max: 0 };
  const ctl = controller({ delayMs: 42000, phases, collected, concurrent });
  await ctl.start();
  await flush();
  for (let i = 0; i < 20 && ctl.state !== "waiting"; i += 1) {
    clock.tick(6000);
    await flush();
  }
  assert.equal(ctl.state, "waiting");
  const stopping = ctl.stop();
  await flush();
  assert.equal(ctl.state, "draining");
  for (let i = 0; i < 30 && ctl.state !== "idle"; i += 1) {
    clock.tick(6000);
    await flush();
  }
  await stopping;
  assert.equal(ctl.state, "idle");
  assert.equal(ctl.inflight, 0);
  const seqs = collected.map((meta) => meta.seq);
  assert.equal(new Set(seqs).size, seqs.length);
  assert.ok(seqs.length >= 1);
  const ordered = phases.filter((phase) => phase === "waiting" || phase === "draining" || phase === "idle");
  assert.ok(ordered.indexOf("waiting") < ordered.indexOf("draining"));
  assert.ok(ordered.indexOf("draining") < ordered.indexOf("idle"));
}

await testNeverWaits();
await testCoupledUploadWaits();
await testStopFlushesTail();
await testStopDuringWaiting();
clock.reset();
console.log("recorder long ok");
