"""dbtest: zen.sqlite3 under the write load of a 2-hour lecture (WAL, checkpoints, lock waits, no loss).

A deterministic seeded schedule of 2400 segments (one every 2-4 s simulated, ~2 h) is replayed
~3000x faster through the real ``Ledger`` (update + final with translation per segment), with an
admin connection writing metrics samples every 5 s simulated and TM units, and short read-only TM
lookups per segment. Scenarios:

- ``baseline``: no long reader.
- ``pinned``: one read transaction held for the whole lecture (admin page / SSE / DB Browser left
  open; QA D13 saw a 443 MB WAL this way).
- ``gapped``: a reader holding a read transaction for 50 segments, then absent for 50, plus an
  admin BEGIN IMMEDIATE held 1.2 s mid-run (e.g. an import) to measure lock waits.
- ``always_on_truncate``: a reader re-opened every 100 segments with no gap (always present) and
  an admin ``wal_checkpoint(TRUNCATE)`` every 300 segments in the brief reader gap.
- opt-in ``always_on`` (no periodic TRUNCATE): demonstrates checkpoint starvation.

Heavier opt-in (``ZEN_LOAD_HEAVY=1``): the app's own 50 ms batch window, slower pacing, and a
6 s admin lock (longer than busy_timeout=5 s, so the ledger's BUSY retry/spool path runs).
At module end one line ``LIVE_LOAD_METRICS={json}`` is printed (capture bypassed) with every
measured number; ``ZEN_LOAD_REPORT=<path>`` also writes the full results as JSON.
Portable: pathlib + pytest tmp_path (tempfile), no POSIX-only calls; runs on Windows.

Asserts are deliberately generous (slow CI); the numbers themselves are the deliverable.
"""
from __future__ import annotations

import json
import os
import platform
import sqlite3
import sys
from pathlib import Path

import pytest

import live_load_sim as sim

HEAVY = (os.environ.get("ZEN_LOAD_HEAVY") or "0").strip() == "1"
P95_BOUND_S = 0.5          # per ledger transaction; real p95 on the box is ~1 ms
ADMIN_P95_BOUND_S = 1.0
WAL_BOUND_BASELINE = 64 * 1024 * 1024   # = journal_size_limit; auto-checkpoint should keep it ~4-8 MB
WAL_AFTER_TRUNCATE = 0

_RESULTS: dict[str, dict] = {}


@pytest.fixture(scope="module")
def workdir(tmp_path_factory):
    return tmp_path_factory.mktemp("live_load")


_KEYS = ("wal_max", "wal_final", "ckpt_resets", "checkpoint", "periodic_truncate", "ledger_lat", "admin_lat",
         "locked", "counts", "expected", "ledger", "max_queue", "db_bytes", "elapsed_s", "sim_span_s")


def metrics_line(results: dict) -> str:
    """One JSON line (prefix LIVE_LOAD_METRICS=) so a laptop run can be captured from the log."""
    payload = {"host": {"platform": platform.platform(), "machine": platform.machine(),
                        "processor": platform.processor(), "cpu_count": os.cpu_count(),
                        "python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version},
               "scenarios": {name: {k: r.get(k) for k in _KEYS} for name, r in results.items()}}
    return "LIVE_LOAD_METRICS=" + json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


@pytest.fixture(scope="module")
def results(workdir, request):
    yield _RESULTS
    if not _RESULTS:
        return
    line = metrics_line(_RESULTS)
    capman = request.config.pluginmanager.getplugin("capturemanager")
    if capman is not None:
        with capman.global_and_fixture_disabled():
            print("\n" + line, flush=True)
    else:
        print("\n" + line, flush=True)
    out = os.environ.get("ZEN_LOAD_REPORT")
    if out:
        Path(out).write_text(json.dumps(_RESULTS, ensure_ascii=False, indent=1), encoding="utf-8")


def _run(results, workdir, sc: sim.Scenario) -> dict:
    if sc.name not in results:
        r = sim.run(sc, workdir).as_dict()
        results[sc.name] = r
    return results[sc.name]


def _assert_no_loss(r: dict) -> None:
    exp, got = r["expected"], r["counts"]
    for key in ("segments", "transcripts", "translations", "events_live", "segments_translated", "metrics", "tm_units"):
        assert got[key] == exp[key], f"{r['name']}: {key} {got[key]} != expected {exp[key]}"
    assert r["ledger"]["ledger_written"] + r["ledger"]["ledger_replayed"] >= exp["ledger_written"], r["ledger"]
    assert r["ledger"]["ledger_dropped"] == 0, r["ledger"]
    assert r["ledger"]["ledger_errors"] == 0, r["ledger"]
    assert exp["metrics"] > 0 and exp["tm_units"] > 0


def _assert_checkpoint_shrinks(r: dict) -> None:
    ck = r["checkpoint"]
    busy, log_frames, done = ck["truncate"]
    assert busy == 0 and log_frames == 0 and done == 0, ck
    assert ck["wal_after_truncate"] == WAL_AFTER_TRUNCATE, ck


def test_baseline_lecture_no_loss_and_bounded_wal(results, workdir):
    r = _run(results, workdir, sim.Scenario("baseline"))
    _assert_no_loss(r)
    assert r["ledger_lat"]["p95_ms"] / 1000 < P95_BOUND_S, r["ledger_lat"]
    assert r["admin_lat"]["p95_ms"] / 1000 < ADMIN_P95_BOUND_S, r["admin_lat"]
    assert r["locked"]["ledger_locked_errors"] == 0 and r["locked"]["admin_locked"] == 0, r["locked"]
    # wal_autocheckpoint=1000 fires during the lecture: the WAL restarted at least once and stays bounded.
    assert r["ckpt_resets"] >= 1, r
    assert r["wal_max"] < WAL_BOUND_BASELINE, r
    # PASSIVE with no reader backfills everything but does not shrink the file; TRUNCATE does.
    busy, log_frames, done = r["checkpoint"]["passive"]
    assert busy == 0 and done == log_frames, r["checkpoint"]
    _assert_checkpoint_shrinks(r)


def test_pinned_reader_wal_grows_until_released(results, workdir):
    base = _run(results, workdir, sim.Scenario("baseline"))
    r = _run(results, workdir, sim.Scenario("pinned", reader="pinned"))
    _assert_no_loss(r)
    assert r["ledger_lat"]["p95_ms"] / 1000 < P95_BOUND_S, r["ledger_lat"]
    # The pinned snapshot blocks every backfill: the WAL never restarts and outgrows the baseline.
    assert r["ckpt_resets"] == 0, r
    assert r["wal_max"] > base["wal_max"], (r["wal_max"], base["wal_max"])
    ck = r["checkpoint"]
    busy, log_frames, done = ck["passive_with_reader"]
    assert log_frames > 0 and done < log_frames, ck        # reader still holds frames back
    _assert_checkpoint_shrinks(r)                           # released -> TRUNCATE empties the WAL


def test_gapped_reader_and_admin_lock(results, workdir):
    pinned = _run(results, workdir, sim.Scenario("pinned", reader="pinned"))
    r = _run(results, workdir, sim.Scenario("gapped", reader="bounded", bounded_every=50, reader_gap=50,
                                            lock_hold_s=1.2))
    _assert_no_loss(r)
    # The 1.2 s admin lock is absorbed by busy_timeout (5 s): no 'database is locked', one slow write.
    assert r["locked"]["ledger_locked_errors"] == 0, r["locked"]
    assert r["ledger_lat"]["p95_ms"] / 1000 < P95_BOUND_S, r["ledger_lat"]
    # The wait is visible, judged relative to the holder's own timestamps (not wall-clock bounds):
    lp = r["lock_probe"]
    assert {"acquired", "release_start", "probe_start", "probe_got_lock"} <= lp.keys(), lp
    held_s = lp["release_start"] - lp["acquired"]
    assert lp["probe_start"] >= lp["acquired"], lp            # probe started while the lock was held
    assert lp["probe_got_lock"] >= lp["release_start"], lp    # and only got it after the holder let go
    remaining_s = lp["release_start"] - lp["probe_start"]
    waited_s = lp["probe_got_lock"] - lp["probe_start"]
    assert waited_s >= remaining_s, (waited_s, remaining_s, held_s)
    # Reader transactions with idle gaps let auto-checkpoint restart the WAL again.
    assert r["ckpt_resets"] >= 1, r
    assert r["wal_max"] < pinned["wal_max"] / 2, (r["wal_max"], pinned["wal_max"])
    _assert_checkpoint_shrinks(r)


def test_always_on_reader_with_periodic_truncate(results, workdir):
    pinned = _run(results, workdir, sim.Scenario("pinned", reader="pinned"))
    # A reader that is (almost) always open starves auto-checkpoint (see the opt-in starvation
    # variant); an admin wal_checkpoint(TRUNCATE) every 300 segments (~15 min) in a reader gap fixes it.
    r = _run(results, workdir, sim.Scenario("always_on_truncate", reader="bounded", bounded_every=100,
                                            reader_gap=0, truncate_every=300))
    _assert_no_loss(r)
    pt = r["periodic_truncate"]
    assert pt["runs"] == 2400 // 300, pt
    assert pt["p95_ms"] / 1000 < ADMIN_P95_BOUND_S, pt
    assert r["wal_max"] < pinned["wal_max"] / 2, (r["wal_max"], pinned["wal_max"])
    _assert_checkpoint_shrinks(r)


@pytest.mark.skipif(not HEAVY, reason="checkpoint-starvation variant: set ZEN_LOAD_HEAVY=1")
def test_always_on_reader_starves_checkpoint(results, workdir):
    r = _run(results, workdir, sim.Scenario("always_on", reader="bounded", bounded_every=100, reader_gap=0))
    _assert_no_loss(r)
    _assert_checkpoint_shrinks(r)           # only an explicit TRUNCATE after the readers stop helps


@pytest.mark.skipif(not HEAVY, reason="heavy live-load variant: set ZEN_LOAD_HEAVY=1 (~1-2 min)")
def test_heavy_realistic_batching_and_long_lock(results, workdir):
    r = _run(results, workdir, sim.Scenario("heavy", reader="bounded", batch_window_s=None,
                                            real_gap_s=0.02, lock_hold_s=6.0))
    _assert_no_loss(r)                        # BUSY beyond busy_timeout -> retry/spool, never a lost row
    assert r["locked"]["ledger_busy_retries"] >= 1 or r["locked"]["admin_locked"] >= 1, r["locked"]
    assert r["ledger_lat"]["p95_ms"] / 1000 < P95_BOUND_S, r["ledger_lat"]
    _assert_checkpoint_shrinks(r)
