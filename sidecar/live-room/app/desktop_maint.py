"""round3 C1: backup + retention for the desktop build.

The Launcher only starts ``app.desktop_service`` (live room); ``app.admin.run`` - where the
daily backup and the retention pass used to live - is only started by the dev script. This
thread gives the desktop build the same maintenance without a second process or the admin
HTTP server:

* backup: ``backup_set`` (VACUUM INTO + integrity verification, main + identity + manifest)
  then ``rotate_backups``, whenever the newest backup is older than ZEN_BACKUP_INTERVAL_H
  (default 24 h; 0 = off). Shared with the admin process through the backup folder itself, so
  if both run, the second sees a fresh backup and skips.
* retention: ``enforce_retention`` daily (ZEN_RETENTION=0 = off); whole-session purges only when
  a backup younger than 24 h exists (same data-loss guard as admin).
* first pass ZEN_MAINT_FIRST_S after start (default 120 s), then every hour. Nothing runs
  when zen.sqlite3 does not exist (ledger off). Failures are logged, never raised.
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("zen.desktop.maint")


def _num(env, name, default):
    try:
        v = float((env.get(name) or "").strip() or default)
        return v if math.isfinite(v) and v >= 0 else default
    except ValueError:
        return default


class DesktopMaintenance:
    def __init__(self, db_path: Path, identity_path: Path | None, backup_dir: Path, *,
                 backup_every_s: float = 86400.0, retention_every_s: float = 86400.0,
                 first_s: float = 120.0, scan_s: float = 3600.0, clock=time.time):
        self.db_path, self.identity_path, self.backup_dir = Path(db_path), identity_path, Path(backup_dir)
        self.backup_every_s, self.retention_every_s = backup_every_s, retention_every_s
        self.first_s, self.scan_s, self.clock = first_s, scan_s, clock
        self._last_retention = None
        self._stop = threading.Event()
        self._thread = None
        self.last = {}

    @classmethod
    def from_env(cls, env=None) -> "DesktopMaintenance":
        from app.admin import db as zdb
        env = os.environ if env is None else env
        hours = _num(env, "ZEN_BACKUP_INTERVAL_H", 24.0)
        return cls(zdb.default_db_path(env), zdb.identity_db_path(env), zdb.backup_dir(env),
                   backup_every_s=hours * 3600.0 if hours > 0 else float("inf"),
                   retention_every_s=86400.0 if (env.get("ZEN_RETENTION") or "0").strip() == "1" else 0.0,
                   first_s=_num(env, "ZEN_MAINT_FIRST_S", 120.0))

    # ---------------------------------------------------------------- one pass
    def backup_due(self) -> bool:
        from app.admin import db as zdb
        if not math.isfinite(self.backup_every_s):
            return False
        age = zdb.latest_backup_age_s(self.backup_dir, self.clock())
        return age is None or age >= self.backup_every_s

    def run_once(self) -> dict:
        from app.admin import db as zdb
        out = {"backup": None, "retention": None}
        if not self.db_path.exists():
            out["skipped"] = "no database"
            self.last = out
            return out
        try:
            if self.backup_due():
                b = zdb.backup_set(self.db_path, self.backup_dir, self.identity_path)
                b["deleted"] = zdb.rotate_backups(self.backup_dir)
                out["backup"] = b
                log.info("desktop backup: %s (rotated %d)", b.get("file"), len(b["deleted"]))
                if zdb.same_disk(self.db_path, self.backup_dir):
                    log.warning("backup folder is on the same disk as the database; set ZEN_BACKUP_DIR")
        except Exception as exc:          # never take the live room down
            out["backup_error"] = str(exc)
            log.exception("desktop backup failed")
        try:
            now = self.clock()
            if self.retention_every_s > 0 and (self._last_retention is None
                                               or now - self._last_retention >= self.retention_every_s):
                from app.admin.retention import enforce_retention
                self._last_retention = now
                age = zdb.latest_backup_age_s(self.backup_dir, now)
                r = enforce_retention(self.db_path, self.identity_path, now=now,
                                      purge_sessions=age is not None and age < 86400.0)
                out["retention"] = r
                log.info("desktop retention: %s", {k: (len(v) if isinstance(v, list) else v) for k, v in r.items()})
        except Exception as exc:
            out["retention_error"] = str(exc)
            log.exception("desktop retention failed")
        self.last = out
        return out

    # ---------------------------------------------------------------- thread
    def _loop(self):
        if self._stop.wait(self.first_s):
            return
        while not self._stop.is_set():
            self.run_once()
            if self._stop.wait(self.scan_s):
                return

    def start(self) -> "DesktopMaintenance":
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="zen-desktop-maint", daemon=True)
            self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
