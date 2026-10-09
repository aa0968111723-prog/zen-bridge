export function createCaptureController(deps) {
  let state = "idle";
  let session = null;
  let pendingRoom = "";
  let seq = 0;
  let timer = null;
  let recorder = null;
  let stream = null;
  let stopRequested = false;
  let generation = 0;
  let lastError = "";
  const released = { tracks: 0, timers: 0, recorders: 0 };
  const uploads = [];
  const inflight = new Set();
  const maxInflight = deps.maxInflight || 2;
  let flightEpoch = 0;
  let timerBusy = false;
  let waitingAt = null;
  const gaps = [];

  function setState(next) {
    if (state === "waiting" && next !== "waiting" && waitingAt !== null && session) {
      gaps.push({ sessionId: session.id, roomId: session.roomId, t0_ms: waitingAt - session.startedAt, t1_ms: Date.now() - session.startedAt, reason: "backpressure" });
      if (gaps.length > 200) gaps.shift();
      waitingAt = null;
    }
    if (next === "waiting" && state !== "waiting") waitingAt = Date.now();
    state = next;
    if (typeof deps.onPhase === "function") {
      try { deps.onPhase(next); } catch { /* host paint */ }
    }
  }

  function closeStream(target) {
    if (!target) return;
    const tracks = target.getTracks();
    for (const track of tracks) track.stop();
    released.tracks += tracks.length;
  }

  function releaseStream() {
    closeStream(stream);
    stream = null;
  }

  function clearTimer() {
    if (!timer) return;
    clearInterval(timer);
    timer = null;
    released.timers += 1;
  }

  function stopRecorder(rec) {
    if (!rec || rec.state === "inactive") return Promise.resolve(null);
    released.recorders += 1;
    return new Promise((resolve) => {
      let settled = false;
      const finish = (blob) => {
        if (settled) return;
        settled = true;
        resolve(blob || null);
      };
      try {
        rec.addEventListener("dataavailable", (ev) => {
          finish(ev && ev.data ? ev.data : null);
        }, { once: true });
      } catch {
        finish(null);
        return;
      }
      try {
        rec.stop();
      } catch {
        finish(null);
      }
    });
  }

  function abandonInflight() {
    flightEpoch += 1;
    inflight.clear();
  }

  function trackUpload(meta, blob) {
    if (!blob || typeof blob.size !== "number" || blob.size <= 0) return Promise.resolve();
    const epoch = flightEpoch;
    const job = Promise.resolve()
      .then(() => deps.upload(meta, blob))
      .then((result) => {
        if (epoch !== flightEpoch) return;
        uploads.push({ meta, bytes: blob.size, result });
        if (uploads.length > 200) uploads.shift();
      }, (err) => {
        if (epoch !== flightEpoch) return;
        if (session && meta.sessionId === session.id) lastError = err?.message || "字幕段上傳失敗";
        try { deps.onUploadError?.(meta, err); } catch { /* host paint */ }
      });
    inflight.add(job);
    return job.finally(() => {
      if (epoch === flightEpoch) inflight.delete(job);
    });
  }

  const schedule = deps.schedule || ((fn, ms) => setTimeout(fn, ms));
  const clearSchedule = deps.clearSchedule || ((id) => clearTimeout(id));
  const uploadTimeoutMs = Number(deps.uploadTimeoutMs) > 0 ? Number(deps.uploadTimeoutMs) : 300000;

  function waitFor(promise, ms) {
    let timer = null;
    let timedOut = false;
    const timeout = new Promise((resolve) => {
      timer = schedule(() => {
        timedOut = true;
        resolve("timeout");
      }, ms);
    });
    return Promise.race([
      Promise.resolve(promise).then(() => "done", () => "done"),
      timeout,
    ]).finally(() => {
      if (timer != null) clearSchedule(timer);
    }).then((result) => result === "timeout" || timedOut);
  }

  async function settleUploads() {
    const deadline = Date.now() + uploadTimeoutMs;
    while (inflight.size) {
      const left = deadline - Date.now();
      if (left <= 0) {
        lastError = lastError || "上傳逾時，已停止等待";
        break;
      }
      const timedOut = await waitFor(Promise.allSettled([...inflight]), left);
      if (timedOut) {
        lastError = lastError || "上傳逾時，已停止等待";
        break;
      }
    }
  }

  function finishCurrent() {
    const current = recorder;
    const meta = current && current._meta;
    const activeSession = session;
    recorder = null;
    if (!current) return Promise.resolve();
    // Stay tracked until the upload itself is queued. Do not await it: the next
    // segment must be able to record while up to maxInflight uploads run.
    const epoch = flightEpoch;
    const job = (async () => {
      try {
        const blob = await stopRecorder(current);
        if (epoch !== flightEpoch) return;
        if (!meta || !activeSession || !blob || typeof blob.size !== "number" || blob.size <= 0) return;
        const t1 = Date.now() - activeSession.startedAt;
        trackUpload({ ...meta, t1_ms: t1 }, blob);
      } catch {
        /* settle still waits for whatever was queued */
      }
    })();
    inflight.add(job);
    return job.finally(() => {
      if (epoch === flightEpoch) inflight.delete(job);
    });
  }

  async function beginSegment(my) {
    if (my !== generation || !stream || !session) return;
    if (inflight.size >= maxInflight && !stopRequested) setState("waiting");
    while (inflight.size >= maxInflight && my === generation && !stopRequested) {
      await Promise.race([...inflight]);
    }
    if (my === generation && !stopRequested && state === "waiting") setState("recording");
    if (my !== generation || stopRequested || state !== "recording" || recorder) return;
    const started = Date.now();
    const meta = {
      sessionId: session.id,
      roomId: session.roomId,
      seq: seq + 1,
      t0_ms: started - session.startedAt,
    };
    recorder = deps.createRecorder(stream);
    recorder._meta = meta;
    recorder.start();
    seq = meta.seq;
    if (typeof recorder.addEventListener === "function") {
      recorder.addEventListener("error", () => {
        if (my === generation && state === "recording") fail("錄音器發生錯誤");
      }, { once: true });
    }
  }

  function armTimer(my) {
    clearTimer();
    timerBusy = false;
    timer = setInterval(() => {
      if (timerBusy || stopRequested || state !== "recording" || my !== generation) return;
      timerBusy = true;
      const busyGen = generation;
      finishCurrent()
        .then(() => {
          if (busyGen !== generation || stopRequested || state !== "recording") return;
          return beginSegment(busyGen);
        })
        .catch(() => {})
        .finally(() => {
          if (busyGen === generation) timerBusy = false;
        });
    }, deps.periodMs || 6000);
  }

  function fail(reason) {
    generation += 1;
    stopRequested = true;
    timerBusy = false;
    lastError = reason || "錄音中斷";
    clearTimer();
    const oldStream = stream;
    stream = null;
    const closing = finishCurrent();
    setState("error");
    closing.finally(() => closeStream(oldStream));
  }

  return {
    get state() { return state; },
    get released() { return released; },
    get uploads() { return uploads; },
    get session() { return session; },
    get pendingRoom() { return pendingRoom; },
    get lastError() { return lastError; },
    get inflight() { return inflight.size; },
    get lastSeq() { return seq; },
    get gaps() { return gaps; },
    canEditRoom() { return state === "idle" || state === "error"; },
    fail,
    async start() {
      if (state === "preparing" || state === "recording" || state === "waiting" || state === "draining") {
        throw new Error("已經在聽，請先停止");
      }
      abandonInflight();
      const my = ++generation;
      const pinnedRoom = String(deps.roomId() || "class");
      const pinnedId = deps.newId();
      pendingRoom = pinnedRoom;
      setState("preparing");
      stopRequested = false;
      lastError = "";
      let mic = null;
      try {
        mic = await deps.openMic();
      } catch (err) {
        if (my !== generation) return;
        releaseStream();
        lastError = err && err.message ? err.message : "無法開始聽";
        setState("error");
        throw err;
      }
      if (my !== generation || stopRequested) {
        closeStream(mic);
        return;
      }
      stream = mic;
      for (const track of stream.getTracks()) {
        track.addEventListener?.("ended", () => {
          if (my === generation && (state === "recording" || state === "waiting")) fail("麥克風中斷或被拔除");
        });
      }
      session = { id: pinnedId, roomId: pinnedRoom, startedAt: Date.now() };
      seq = 0;
      setState("recording");
      await beginSegment(my);
      if (my !== generation || stopRequested || state !== "recording") return;
      armTimer(my);
    },
    async stop() {
      if (state !== "recording" && state !== "waiting" && state !== "preparing" && state !== "draining") return;
      const my = generation;
      generation += 1;
      stopRequested = true;
      timerBusy = false;
      setState("draining");
      clearTimer();
      try {
        const timedOut = await waitFor(finishCurrent(), uploadTimeoutMs);
        if (timedOut) lastError = lastError || "上傳逾時，已停止等待";
        releaseStream();
        recorder = null;
        await settleUploads();
      } finally {
        if (state === "draining" && generation === my + 1) setState("idle");
      }
    },
  };
}
