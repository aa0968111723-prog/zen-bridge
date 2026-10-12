"""Write-load simulator for zen.sqlite3 during a 2-hour lecture (dbtest; used by test_sqlite_live_load).

Drives the real modules against a temp DB: ``app.admin.migrate`` creates the schema,
``app.ledger.Ledger`` is the single caption writer (update + final/translation per segment),
``app.admin.observability.persist_sample`` writes metrics rows on a second (admin) connection,
``app.tm.add_unit`` adds TM units and ``app.tm.TranslationMemory`` does short read-only lookups.

Time is compressed: a seeded schedule of segments 2-4 s apart (simulated) is replayed with a
small real sleep per segment. Nothing here touches the network, a model, or a real data dir.
"""
from __future__ import annotations

import os
import random
import sqlite3
import statistics
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from app import ledger as ledger_mod
from app.admin import db as zdb
from app.admin import migrate as zmigrate
from app.admin.observability import persist_sample
from app.tm import TranslationMemory, add_unit

METRIC_SET = ("rtf", "backlog_s", "pending", "inflight", "translate_queued", "listeners")
_ZH = ("因緣和合", "諸法無我", "緣起性空", "阿彌陀佛", "菩提心", "般若波羅蜜", "念念分明", "放下執著")


def wal_path(db: Path) -> Path:
    return db.with_name(db.name + "-wal")


def wal_size(db: Path) -> int:
    try:
        return os.path.getsize(wal_path(db))
    except OSError:
        return 0


def wal_ckpt_seq(db: Path) -> int | None:
    """Checkpoint sequence number from the WAL header (bytes 12-15, big endian). It increases
    every time a writer restarts the WAL after a complete checkpoint, so a change means an
    (auto-)checkpoint fully backfilled and the log wrapped. Read-only: never disturbs SQLite."""
    try:
        with open(wal_path(db), "rb") as fh:
            head = fh.read(32)
    except OSError:
        return None
    if len(head) < 32:
        return None
    return struct.unpack(">I", head[12:16])[0]


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def schedule(n: int, seed: int = 20261010) -> list[float]:
    """Deterministic simulated start times (s): one segment every uniform(2, 4) s."""
    rng = random.Random(seed)
    t, out = 0.0, []
    for _ in range(n):
        out.append(t)
        t += rng.uniform(2.0, 4.0)
    return out


class TimedLedger(ledger_mod.Ledger):
    """The real Ledger; only records how long each real _write_batch transaction takes."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.lat: list[float] = []
        self.batch_sizes: list[int] = []
        self.locked_errors = 0

    def _write_batch(self, conn, batch):
        t0 = time.perf_counter()
        try:
            super()._write_batch(conn, batch)
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                self.locked_errors += 1
            raise
        self.lat.append(time.perf_counter() - t0)
        self.batch_sizes.append(len(batch))


@dataclass
class Scenario:
    name: str
    segments: int = 2400
    reader: str = "none"            # none | pinned (one read txn for the whole run) | bounded
    bounded_every: int = 100        # bounded reader: close its read txn after N segments
    reader_gap: int = 0             # bounded reader: segments with no reader before it re-opens
    truncate_every: int = 0         # admin wal_checkpoint(TRUNCATE) every N segments (in a reader gap)
    real_gap_s: float = 0.001       # real sleep per segment (~3 s simulated -> ~3000x compression)
    batch_window_s: float | None = 0.001   # None = keep the app's BATCH_WINDOW_S
    lock_hold_s: float = 0.0        # an admin BEGIN IMMEDIATE held this long mid-run (0 = none)
    metrics_every_s: float = 5.0    # simulated seconds between metrics samples
    tm_every: int = 40              # add_unit every N segments (admin correction -> TM)
    seed: int = 20261010


@dataclass
class Result:
    name: str
    expected: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)
    ledger: dict = field(default_factory=dict)
    wal_max: int = 0
    wal_final: int = 0
    wal_samples: int = 0
    db_bytes: int = 0
    ckpt_seq_start: int | None = None
    ckpt_seq_end: int | None = None
    ckpt_resets: int = 0
    ledger_lat: dict = field(default_factory=dict)
    admin_lat: dict = field(default_factory=dict)
    locked: dict = field(default_factory=dict)
    max_queue: int = 0
    checkpoint: dict = field(default_factory=dict)
    periodic_truncate: dict = field(default_factory=dict)
    trunc_samples_s: list = field(default_factory=list)   # periodic TRUNCATE product seconds (machine pauses removed)
    lock_probe: dict = field(default_factory=dict)
    wal_bytes_per_commit: float = 0.0
    elapsed_s: float = 0.0
    sim_span_s: float = 0.0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _lat(values: list[float]) -> dict:
    return {"n": len(values), "p50_ms": round(pct(values, 0.5) * 1000, 3),
            "p95_ms": round(pct(values, 0.95) * 1000, 3), "max_ms": round(max(values or [0]) * 1000, 3),
            "mean_ms": round((statistics.fmean(values) if values else 0) * 1000, 3)}


class SharedMachinePauseClock:
    """Wall time minus a sudden pause of the shared machine.

    A daemon thread sleeps ``quantum_s`` and timestamps each wake. A wake later
    than ``quantum_s + stall_s`` means this process was not scheduled. That gap
    is a runner stall, not checkpoint work.

    ``sqlite3`` releases the GIL inside ``wal_checkpoint``, and ``thread_time``
    stays flat while the call waits on disk. The heartbeat keeps running
    through that wait, so the wait stays in the sample. Time is removed only
    when the heartbeat missed a wake and this thread was off CPU for the same
    interval. A GIL-holding or genuinely slow checkpoint still counts in full.
    """

    def __init__(self, quantum_s: float = 0.02, stall_s: float = 0.1):
        if quantum_s <= 0 or stall_s <= 0:
            raise ValueError("quantum_s and stall_s must be positive")
        self.quantum_s = float(quantum_s)
        self.stall_s = float(stall_s)
        self._stalls: list[tuple[float, float]] = []
        self._last_wake = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "SharedMachinePauseClock":
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="machine-pause-clock", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    def _run(self) -> None:
        last = time.perf_counter()
        with self._lock:
            self._last_wake = last
        while not self._stop.wait(self.quantum_s):
            now = time.perf_counter()
            self.observe_gap(last, now)
            last = now

    def observe_gap(self, last: float, now: float) -> None:
        """Record one heartbeat gap. ``last`` and ``now`` are ``perf_counter`` seconds."""
        late = (now - last) - self.quantum_s
        with self._lock:
            self._last_wake = max(self._last_wake, now)
            if late >= self.stall_s and now >= last:
                # The sleep was due at last+quantum. Only the extra is the pause.
                self._stalls.append((now - late, now))

    def wait_until(self, mark: float) -> None:
        """Wait until a heartbeat wake has been published at or after ``mark``.

        The pause that ends as the checkpoint returns is visible only after the
        heartbeat runs. This wait is outside the sample.
        """
        if self._thread is None:
            return
        deadline = time.perf_counter() + 1.0 + self.quantum_s
        while True:
            with self._lock:
                if self._last_wake >= mark:
                    return
            if time.perf_counter() >= deadline:
                return
            time.sleep(0.005)

    def overlap_s(self, t0: float, t1: float) -> float:
        total = 0.0
        with self._lock:
            spans = list(self._stalls)
        for start, end in spans:
            lo = max(start, t0)
            hi = min(end, t1)
            if hi > lo:
                total += hi - lo
        return total

    def product_s(self, t0: float, t1: float, cpu_s: float) -> float:
        """Seconds of the window that are checkpoint work, not a machine pause."""
        wall = max(0.0, t1 - t0)
        off_cpu = max(0.0, wall - max(0.0, cpu_s))
        paused = min(self.overlap_s(t0, t1), off_cpu)
        return wall - paused


def _caption(i: int, sim_t: float, room: str = "load-room", sess: str = "load-s1") -> tuple[dict, dict]:
    zh = f"第{i}句 {_ZH[i % len(_ZH)]} {_ZH[(i * 7) % len(_ZH)]}，請大家一起思惟。"
    base = {"id": f"{room}:{sess}:{i}", "room_id": room, "session_id": sess, "seq": i,
            "t0_ms": int(sim_t * 1000), "t1_ms": int(sim_t * 1000) + 1800}
    update = {**base, "type": "update", "status": "zh_ready", "zh": zh}
    final = {**base, "type": "final", "status": "ready", "zh": zh, "en": f"Sentence {i}: dependent origination.",
             "translate_status": "ok"}
    return update, final


def run(sc: Scenario, workdir: Path) -> Result:
    workdir.mkdir(parents=True, exist_ok=True)
    db = workdir / f"{sc.name}.sqlite3"
    # Call migrate() directly: migrate.main() prints CJK to stdout and crashes on a cp1252 Windows console.
    assert zmigrate.migrate(str(db)) >= 1
    res = Result(sc.name)
    times = schedule(sc.segments, sc.seed)
    res.sim_span_s = round(times[-1], 1)

    old_window = ledger_mod.BATCH_WINDOW_S
    if sc.batch_window_s is not None:
        ledger_mod.BATCH_WINDOW_S = sc.batch_window_s
    led = TimedLedger(db)
    admin = zdb.connect(db)                      # admin process writer: metrics + TM units
    tm = TranslationMemory(lambda: zdb.connect(db, readonly=True, timeout_ms=1000))
    admin_lat: list[float] = []
    admin_locked = tm_locked = 0
    reader = None
    lock_thread = None
    metric_rows = tm_units = 0
    last_metric = -1e9
    seq_seen: set[int] = set()
    reopen_at = None
    trunc_lat: list[float] = []
    trunc_paused: list[float] = []
    trunc_busy = 0
    pause_clock: SharedMachinePauseClock | None = None
    trunc_wal_before: list[int] = []
    redeliveries = 0

    def sample_wal():
        res.wal_max = max(res.wal_max, wal_size(db))
        res.wal_samples += 1
        s = wal_ckpt_seq(db)
        if s is not None:
            if res.ckpt_seq_start is None:
                res.ckpt_seq_start = s
            seq_seen.add(s)
            res.ckpt_seq_end = s

    def open_reader():
        r = zdb.connect(db, readonly=True)
        r.execute("BEGIN")
        r.execute("SELECT count(*) FROM segments").fetchone()   # takes the read snapshot
        return r

    def admin_write(fn) -> None:
        nonlocal admin_locked
        t0 = time.perf_counter()
        try:
            admin.execute("BEGIN IMMEDIATE")
            fn(admin)
            admin.execute("COMMIT")
            admin_lat.append(time.perf_counter() - t0)
        except sqlite3.OperationalError as exc:
            if admin.in_transaction:
                admin.execute("ROLLBACK")
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                admin_locked += 1
            raise

    lock_acquired = threading.Event()
    lock_times: dict = {}

    def hold_lock():
        c = zdb.connect(db)
        try:
            c.execute("BEGIN IMMEDIATE")
            c.execute("INSERT INTO metrics(ts, name, value) VALUES (?,?,?)", (time.time(), "admin_import", 1.0))
            lock_times["acquired"] = time.perf_counter()
            lock_acquired.set()                  # coordination point: the write lock is now really held
            time.sleep(sc.lock_hold_s)
            lock_times["release_start"] = time.perf_counter()   # taken before COMMIT releases the lock
            c.execute("COMMIT")
        finally:
            lock_acquired.set()                  # never leave the main loop waiting on a failed holder
            c.close()

    def probe_lock_wait() -> None:
        # A dedicated writer that starts only after the holder owns the lock. It cannot get the
        # lock before COMMIT, so its wait is measured against the holder's own timestamps instead
        # of an absolute wall-clock threshold.
        p = zdb.connect(db)
        try:
            t0 = time.perf_counter()
            probe_retries = 0
            # The heavy holder intentionally exceeds busy_timeout. The observer
            # must survive that expected BUSY to measure the eventual release;
            # otherwise the simulation aborts before checking ledger recovery.
            deadline = t0 + sc.lock_hold_s + 10
            while True:
                try:
                    p.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError as exc:
                    if not any(word in str(exc).lower() for word in ('locked', 'busy')) or time.perf_counter() >= deadline:
                        raise
                    probe_retries += 1
                    time.sleep(0.05)
            t1 = time.perf_counter()
            p.execute("ROLLBACK")
        finally:
            p.close()
        res.lock_probe = {"probe_start": t0, "probe_got_lock": t1, "probe_busy_retries": probe_retries, **lock_times}

    started = time.perf_counter()
    try:
        if sc.truncate_every:
            pause_clock = SharedMachinePauseClock().start()
        if sc.reader in ("pinned", "bounded"):
            reader = open_reader()
        for i, sim_t in enumerate(times):
            update, final = _caption(i, sim_t)
            led.submit(update)
            try:
                tm.exact(update["zh"]) if i % 10 else tm.fuzzy(update["zh"])
            except sqlite3.OperationalError:
                tm_locked += 1
            led.submit(final)
            if sim_t - last_metric >= sc.metrics_every_s:
                last_metric = sim_t
                vals = {k: float((i * 31 + j) % 97) / 10 for j, k in enumerate(METRIC_SET)}
                for attempt in range(3):     # an admin sampler would retry next tick; never lose a row here
                    try:
                        admin_write(lambda c: persist_sample(c, 1.79e9 + sim_t, vals))
                        metric_rows += len(vals)
                        break
                    except sqlite3.OperationalError:
                        time.sleep(0.05)
            if sc.tm_every and i % sc.tm_every == sc.tm_every - 1:
                zh = update["zh"]
                try:
                    admin_write(lambda c: add_unit(c, zh, f"TM {i}", origin="correction", room_id="load-room"))
                    tm_units += 1
                except sqlite3.OperationalError:
                    pass
            if sc.reader == "bounded":
                if reader is not None and i % sc.bounded_every == sc.bounded_every - 1:
                    reader.execute("COMMIT")
                    reader.close()
                    reader = None
                    reopen_at = i + sc.reader_gap
                    if sc.truncate_every and (i + 1) % sc.truncate_every == 0:
                        trunc_wal_before.append(wal_size(db))
                        t0 = time.perf_counter()
                        cpu0 = time.thread_time()
                        row = zdb.try_checkpoint(admin)
                        # The ledger may own the checkpoint lock at this exact
                        # gap. Give that short-lived owner a bounded chance to
                        # finish before reopening the next read snapshot. Count
                        # the entire retry in the latency gate. A pause of the
                        # shared machine is not checkpoint work: the heartbeat
                        # misses that interval, and it leaves the sample.
                        while row[0] and time.perf_counter() - t0 < 0.2:
                            time.sleep(0.005)
                            row = zdb.try_checkpoint(admin)
                        t1 = time.perf_counter()
                        cpu1 = time.thread_time()
                        if pause_clock is None:
                            product = t1 - t0
                        else:
                            pause_clock.wait_until(t1)
                            product = pause_clock.product_s(t0, t1, cpu1 - cpu0)
                        trunc_lat.append(product)
                        trunc_paused.append(max(0.0, (t1 - t0) - product))
                        trunc_busy += int(row[0] != 0)
                if reader is None and reopen_at is not None and i >= reopen_at:
                    reader = open_reader()
                    reopen_at = None
            if sc.lock_hold_s and i == sc.segments // 2:
                lock_thread = threading.Thread(target=hold_lock, daemon=True)
                lock_thread.start()
                assert lock_acquired.wait(30), "admin lock holder never acquired the lock"
                if "acquired" in lock_times:
                    if sc.lock_hold_s > 5:
                        # Guarantee real ledger work during the long lock. The
                        # regular queue may already have drained before the lock
                        # holder wins. Re-delivery also verifies idempotence:
                        # this final caption must remain one DB row, not two.
                        led.submit(dict(final))
                        redeliveries += 1
                    probe_lock_wait()
            res.max_queue = max(res.max_queue, led._q.qsize())
            if led._q.qsize() > 1000:            # slow CI: back off instead of overflowing the 2000 queue
                led.wait_idle(30)
            if i % 10 == 0:
                sample_wal()
            if sc.real_gap_s:
                time.sleep(sc.real_gap_s)
        if lock_thread is not None:
            lock_thread.join(60)
        assert led.wait_idle(120), "ledger did not drain"
        sample_wal()
        res.wal_final = wal_size(db)
        res.ckpt_resets = len(seq_seen) - 1 if seq_seen else 0

        # ---- checkpoint behaviour at the end of the lecture
        ck = {}
        probe = zdb.connect(db)
        try:
            ck["passive_with_reader"] = None
            if reader is not None:
                ck["passive_with_reader"] = list(probe.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone())
                ck["wal_after_passive_with_reader"] = wal_size(db)
                reader.execute("COMMIT")
                reader.close()
                reader = None
            ck["passive"] = list(probe.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone())
            ck["wal_after_passive"] = wal_size(db)
            ck["truncate"] = list(probe.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
            ck["wal_after_truncate"] = wal_size(db)
        finally:
            probe.close()
        res.checkpoint = ck
    finally:
        if pause_clock is not None:
            pause_clock.stop()
        res.elapsed_s = round(time.perf_counter() - started, 2)
        if reader is not None:
            try:
                reader.close()
            except Exception:
                pass
        led.close(10)
        admin.close()
        ledger_mod.BATCH_WINDOW_S = old_window

    c = sqlite3.connect(db)
    try:
        q = lambda sql: c.execute(sql).fetchone()[0]  # noqa: E731
        res.counts = {
            "segments": q("SELECT count(*) FROM segments"),
            "transcripts": q("SELECT count(*) FROM transcripts"),
            "translations": q("SELECT count(*) FROM translations"),
            "events_live": q("SELECT count(*) FROM events WHERE kind LIKE 'live.%'"),
            "metrics": q("SELECT count(*) FROM metrics WHERE name <> 'admin_import'"),
            "tm_units": q("SELECT count(*) FROM tm_units"),
            "segments_translated": q("SELECT count(*) FROM segments WHERE status='translated'"),
        }
    finally:
        c.close()
    res.db_bytes = os.path.getsize(db)
    n = sc.segments
    # Audit events record deliveries; canonical caption tables must still have
    # only n rows even when the heavy case re-delivers one final event.
    res.expected = {"segments": n, "transcripts": n, "translations": n, "events_live": n + redeliveries,
                    "metrics": metric_rows, "tm_units": tm_units, "segments_translated": n,
                    "ledger_written": 2 * n + redeliveries}
    res.ledger = {**led.stats(), "batches": len(led.batch_sizes),
                  "mean_batch": round(statistics.fmean(led.batch_sizes), 2) if led.batch_sizes else 0}
    res.ledger_lat = _lat(led.lat)
    if sc.truncate_every:
        res.trunc_samples_s = list(trunc_lat)
        res.periodic_truncate = {"runs": len(trunc_lat), "busy": trunc_busy, **_lat(trunc_lat),
                                 "machine_pause_ms": round(sum(trunc_paused) * 1000, 3),
                                 "wal_before_max": max(trunc_wal_before or [0])}
    if sc.reader == "pinned" and led.batch_sizes:
        res.wal_bytes_per_commit = round(res.wal_max / len(led.batch_sizes), 1)
    res.admin_lat = _lat(admin_lat)
    res.locked = {"ledger_locked_errors": led.locked_errors, "ledger_busy_retries": led.retries,
                  "admin_locked": admin_locked, "tm_reader_locked": tm_locked}
    return res
