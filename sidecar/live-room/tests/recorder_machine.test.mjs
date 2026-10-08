import assert from "node:assert/strict";
import { createCaptureController } from "../app/static/recorder_machine.js";

function fakeStream() {
  const tracks = [{ stop() { tracks.stopped = (tracks.stopped || 0) + 1; } }];
  return { getTracks: () => tracks };
}

function fakeRecorderFactory() {
  const made = [];
  function createRecorder() {
    const listeners = {};
    const rec = {
      state: "inactive",
      addEventListener(name, fn) { listeners[name] = fn; },
      start() { this.state = "recording"; },
      stop() {
        this.state = "inactive";
        listeners.dataavailable?.({ data: { size: 1200, complete: true } });
        listeners.stop?.();
      },
      requestData() {
        listeners.dataavailable?.({ data: { size: 1200, complete: false } });
      },
    };
    made.push(rec);
    return rec;
  }
  return { createRecorder, made };
}

const factory = fakeRecorderFactory();
const uploads = [];
const ctl = createCaptureController({
  periodMs: 60000,
  openMic: async () => fakeStream(),
  createRecorder: factory.createRecorder,
  newId: () => "sess",
  roomId: () => "class",
  upload: (meta, data) => uploads.push({ meta, data }),
});

await ctl.start();
assert.equal(ctl.state, "recording");
await assert.rejects(() => ctl.start(), /已經在聽/);
assert.equal(factory.made.length, 1);
factory.made[0].requestData();
assert.equal(uploads.length, 0, "中間切片不能當成獨立音檔上傳");
await ctl.stop();
assert.equal(ctl.state, "idle");
assert.equal(uploads.length, 1);
assert.equal(uploads[0].meta.seq, 1);
assert.equal(uploads[0].meta.sessionId, "sess");
assert.ok(ctl.released.tracks >= 1);
assert.ok(ctl.released.timers >= 1);

const late = [];
let openRelease;
const preparing = createCaptureController({
  periodMs: 60000,
  openMic: () => new Promise((resolve) => { openRelease = resolve; }),
  createRecorder: factory.createRecorder,
  newId: () => "late",
  roomId: () => "class",
  upload: () => {},
});
const starting = preparing.start();
await Promise.resolve();
assert.equal(preparing.state, "preparing");
await preparing.stop();
const lateStream = fakeStream();
late.push(lateStream);
openRelease(lateStream);
await starting;
assert.equal(preparing.state, "idle");
assert.ok(lateStream.getTracks().stopped >= 1, "取消後才到達的麥克風必須關閉");

let uploadRelease;
const draining = createCaptureController({
  periodMs: 60000,
  openMic: async () => fakeStream(),
  createRecorder: factory.createRecorder,
  newId: () => "drain",
  roomId: () => "class",
  upload: () => new Promise((resolve) => { uploadRelease = resolve; }),
});
await draining.start();
const stopping = draining.stop();
await new Promise((resolve) => setTimeout(resolve, 0));
assert.equal(draining.state, "draining");
assert.equal(draining.canEditRoom(), false);
uploadRelease();
await stopping;
assert.equal(draining.state, "idle");
assert.equal(draining.uploads.length, 1);
assert.equal(draining.canEditRoom(), true);

const emptyFactory = fakeRecorderFactory();
const emptyCtl = createCaptureController({
  periodMs: 60000,
  openMic: async () => fakeStream(),
  createRecorder() {
    const rec = emptyFactory.createRecorder();
    const orig = rec.stop.bind(rec);
    rec.stop = () => {
      rec.state = "inactive";
      const listeners = rec;
      orig();
    };
    return rec;
  },
  newId: () => "empty",
  roomId: () => "locked-room",
  upload: () => { throw new Error("空音檔不該上傳"); },
});
const madeBefore = emptyFactory.made.length;
await emptyCtl.start();
const rec = emptyFactory.made[madeBefore];
rec.stop = () => {
  rec.state = "inactive";
  rec.addEventListener = rec.addEventListener;
};
const listeners = {};
rec.addEventListener = (name, fn) => { listeners[name] = fn; };
rec.stop = () => {
  rec.state = "inactive";
  listeners.dataavailable?.({ data: { size: 0, complete: true } });
};
await emptyCtl.stop();
assert.equal(emptyCtl.uploads.length, 0);
assert.equal(emptyCtl.session.roomId, "locked-room");

let roomName = "pinned";
let releasePin;
const pinUploads = [];
const pinCtl = createCaptureController({
  periodMs: 60000,
  openMic: () => new Promise((resolve) => { releasePin = resolve; }),
  createRecorder: factory.createRecorder,
  newId: () => "pin-sess",
  roomId: () => roomName,
  upload: (meta, data) => pinUploads.push({ meta, data }),
});
const pinStart = pinCtl.start();
await Promise.resolve();
roomName = "hijack";
releasePin(fakeStream());
await pinStart;
assert.equal(pinCtl.state, "recording");
assert.equal(pinCtl.session.roomId, "pinned");
assert.equal(pinCtl.session.id, "pin-sess");
await pinCtl.stop();
assert.equal(pinUploads.length, 1);
assert.equal(pinUploads[0].meta.roomId, "pinned");
assert.equal(pinCtl.canEditRoom(), true);

let gateA;
let gateB;
let phase = "a";
const race = createCaptureController({
  periodMs: 60000,
  openMic: () => new Promise((resolve) => {
    if (phase === "a") gateA = resolve;
    else gateB = resolve;
  }),
  createRecorder: factory.createRecorder,
  newId: () => phase,
  roomId: () => "class",
  upload: () => {},
});
const firstStart = race.start();
await Promise.resolve();
await race.stop();
assert.equal(race.state, "idle");
phase = "b";
const secondStart = race.start();
await Promise.resolve();
assert.equal(race.state, "preparing");
const lateMic = fakeStream();
gateA(lateMic);
await firstStart;
assert.equal(race.state, "preparing");
assert.ok(lateMic.getTracks().stopped >= 1);
assert.notEqual(race.session && race.session.id, "a");
const liveMic = fakeStream();
gateB(liveMic);
await secondStart;
assert.equal(race.state, "recording");
assert.equal(race.session.id, "b");
assert.equal(race.session.roomId, "class");
await race.stop();
assert.equal(race.state, "idle");

const boom = createCaptureController({
  periodMs: 60000,
  openMic: async () => fakeStream(),
  createRecorder: factory.createRecorder,
  newId: () => "boom",
  roomId: () => "class",
  upload: async () => { throw new Error("上傳失敗"); },
});
await boom.start();
await boom.stop();
assert.equal(boom.state, "idle");
assert.equal(boom.canEditRoom(), true);
assert.equal(boom.uploads.length, 0);
assert.equal(boom.lastError, "上傳失敗");

const flagged = [];
const flaggedRecs = [];
const flagCtl = createCaptureController({
  periodMs: 60000,
  openMic: async () => fakeStream(),
  createRecorder() {
    const listeners = {};
    const rec = {
      state: "inactive",
      addEventListener(name, fn) { listeners[name] = fn; },
      start() { this.state = "recording"; },
      stop() {
        this.state = "inactive";
        listeners.dataavailable?.({ data: { size: 80, complete: false } });
      },
      requestData() {
        listeners.dataavailable?.({ data: { size: 80, complete: true } });
      },
    };
    flaggedRecs.push(rec);
    return rec;
  },
  newId: () => "flag",
  roomId: () => "class",
  upload: (meta, data) => flagged.push({ meta, data }),
});
await flagCtl.start();
flaggedRecs[0].requestData();
assert.equal(flagged.length, 0, "requestData 的 complete 不能觸發上傳");
await flagCtl.stop();
assert.equal(flagged.length, 1);
assert.equal(flagged[0].data.size, 80);
assert.equal(flagged[0].data.complete, false);

const capped = [];
const cappedRecs = [];
const cappedCtl = createCaptureController({
  periodMs: 15,
  maxInflight: 2,
  openMic: async () => fakeStream(),
  createRecorder() {
    const listeners = {};
    const rec = {
      state: "inactive",
      addEventListener(name, fn) { listeners[name] = fn; },
      start() { this.state = "recording"; },
      stop() {
        this.state = "inactive";
        listeners.dataavailable?.({ data: { size: 90, complete: true } });
      },
    };
    cappedRecs.push(rec);
    return rec;
  },
  newId: () => "cap",
  roomId: () => "class",
  upload: (meta) => new Promise((resolve) => { capped.push({ meta, resolve }); }),
});
try {
  await cappedCtl.start();
  const deadline = Date.now() + 1000;
  while (capped.length < 2 && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  assert.equal(capped.length, 2);
  assert.deepEqual(capped.map((item) => item.meta.seq), [1, 2]);
  await new Promise((resolve) => setTimeout(resolve, 40));
  assert.equal(capped.length, 2, "滿載時不能再塞第三段上傳");
  assert.equal(cappedCtl.state, "waiting", "不能把沒有錄音器的等待顯示為正常錄音");
  assert.ok(cappedCtl.inflight <= 2);
  assert.ok(cappedRecs.filter((rec) => rec.state === "recording").length <= 1);
  assert.equal(cappedCtl.canEditRoom(), false);
} finally {
  const stopping = cappedCtl.stop();
  await new Promise((resolve) => setTimeout(resolve, 0));
  for (const item of capped) item.resolve();
  await stopping;
}
assert.equal(cappedCtl.state, "idle");
assert.ok(cappedCtl.gaps.length >= 1);
assert.equal(cappedCtl.gaps[0].reason, "backpressure");
assert.ok(cappedCtl.gaps[0].t1_ms >= cappedCtl.gaps[0].t0_ms);

const failedStream = fakeStream();
let failedRelease;
const failedCtl = createCaptureController({
  periodMs: 60000,
  openMic: async () => failedStream,
  createRecorder: factory.createRecorder,
  newId: () => "fail-sess",
  roomId: () => "class",
  upload: () => new Promise((resolve) => { failedRelease = resolve; }),
});
await failedCtl.start();
failedCtl.fail("麥克風中斷或被拔除");
assert.equal(failedCtl.state, "error");
assert.equal(failedCtl.canEditRoom(), true);
await new Promise((resolve) => setTimeout(resolve, 0));
assert.ok(failedStream.getTracks().stopped >= 1);
assert.equal(failedCtl.inflight, 1);
failedRelease();
await new Promise((resolve) => setTimeout(resolve, 0));
assert.equal(failedCtl.uploads.length, 1);
assert.equal(failedCtl.uploads[0].meta.sessionId, "fail-sess");

async function testStopNeverHangsWhenUploadStalls() {
  const pending = [];
  const stall = createCaptureController({
    periodMs: 60000,
    uploadTimeoutMs: 5000,
    schedule(fn, ms) {
      const item = { fn, ms, cleared: false };
      pending.push(item);
      return item;
    },
    clearSchedule(item) {
      if (item) item.cleared = true;
    },
    openMic: async () => fakeStream(),
    createRecorder: factory.createRecorder,
    newId: () => "stall",
    roomId: () => "class",
    upload: () => new Promise(() => {}),
  });
  await stall.start();
  const stopping = stall.stop();
  const winner = await Promise.race([
    (async () => {
      const deadline = Date.now() + 1000;
      while (Date.now() < deadline) {
        // finishCurrent and settleUploads each arm their own timer. Fire every one that is still pending.
        for (const timer of pending) {
          if (!timer.cleared) timer.fn();
        }
        const status = await Promise.race([
          stopping.then(() => "done"),
          new Promise((resolve) => setTimeout(() => resolve("wait"), 0)),
        ]);
        if (status === "done") return "done";
      }
      return "hung-inner";
    })(),
    new Promise((resolve) => setTimeout(() => resolve("hung"), 500)),
  ]);
  assert.equal(winner, "done");
  assert.equal(stall.state, "idle");
  assert.ok(stall.inflight >= 1, "a stalled upload must not be required to resolve");
  assert.match(stall.lastError, /逾時/);
}
await testStopNeverHangsWhenUploadStalls();

async function testTimedOutUploadDoesNotWedgeNextSession() {
  let releaseOld = () => {};
  const seen = [];
  let sessionNum = 0;
  const ctl = createCaptureController({
    periodMs: 60000,
    maxInflight: 1,
    uploadTimeoutMs: 40,
    openMic: async () => fakeStream(),
    createRecorder: factory.createRecorder,
    newId: () => "sess-" + (++sessionNum),
    roomId: () => "class",
    upload(meta) {
      seen.push(meta.sessionId + ":" + meta.seq);
      if (meta.sessionId === "sess-1") {
        return new Promise((resolve) => { releaseOld = resolve; });
      }
      return Promise.resolve({ ok: true });
    },
  });
  await ctl.start();
  const stopping = ctl.stop();
  const winner = await Promise.race([
    stopping.then(() => "done"),
    new Promise((resolve) => setTimeout(() => resolve("hung"), 500)),
  ]);
  assert.equal(winner, "done");
  assert.equal(ctl.state, "idle");
  assert.ok(ctl.inflight >= 1, "timed-out upload stays tracked until the next start");
  const started = ctl.start();
  const startWinner = await Promise.race([
    started.then(() => "started"),
    new Promise((resolve) => setTimeout(() => resolve("wedged"), 200)),
  ]);
  assert.equal(startWinner, "started");
  assert.equal(ctl.state, "recording");
  assert.equal(ctl.inflight, 0);
  const stoppingNext = ctl.stop();
  const nextWinner = await Promise.race([
    stoppingNext.then(() => "done"),
    new Promise((resolve) => setTimeout(() => resolve("hung"), 500)),
  ]);
  assert.equal(nextWinner, "done");
  assert.ok(seen.includes("sess-2:1"), seen.join(","));
  assert.equal(ctl.inflight, 0);
  const uploadsBefore = ctl.uploads.length;
  releaseOld({ ok: true });
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(ctl.inflight, 0);
  assert.equal(ctl.uploads.length, uploadsBefore);
  await ctl.start();
  await ctl.stop();
  assert.ok(seen.includes("sess-3:1"), seen.join(","));
  assert.equal(ctl.inflight, 0);
}
await testTimedOutUploadDoesNotWedgeNextSession();
console.log("recorder machine ok");
