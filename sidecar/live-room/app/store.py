from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.textutil import scrub_caption

log = logging.getLogger("breeze.store")


def _flags_text(value) -> str | None:
    if not isinstance(value, list) or not value:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _caption_dict(row) -> dict:
    item = dict(row)
    raw = item.pop("term_flags", None)
    if not raw:
        return item
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return item
    if isinstance(parsed, list) and parsed:
        item["term_flags"] = parsed
    return item


def _glossary_row(row) -> dict | None:
    if not row:
        return None
    room_id, version, payload, updated_at = row[0], row[1], row[2], row[3]
    try:
        terms = json.loads(payload or "[]")
    except json.JSONDecodeError:
        log.warning("room glossary for %s was unreadable", room_id)
        return None
    if not isinstance(terms, list):
        return None
    try:
        version_n = int(version or 0)
        stamp = float(updated_at or 0)
    except (TypeError, ValueError):
        return None
    return {"room_id": str(room_id or ""), "version": version_n, "terms": terms, "updated_at": stamp}


class CaptionStore:
    """Optional single-machine caption store. Audio files are not kept.

    Writes share one connection on one thread, in call order, so a delete cannot
    be overtaken by a save that was queued first. Readers wait for that queue.
    """

    def __init__(self, path: str | Path | None):
        text = "" if path is None else str(path).strip()
        self.path = Path(text) if text else None
        self.enabled = self.path is not None
        self.errors = 0
        self.recovered = False
        self.quarantine_path: Path | None = None
        self._conn: sqlite3.Connection | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._writer: threading.Thread | None = None
        self._closed = False
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="breeze-store")
        self._submit(self._open).result()

    def _on_writer(self) -> bool:
        return self._writer is not None and threading.current_thread() is self._writer

    def _submit(self, fn, *args):
        if self._pool is None:
            raise RuntimeError("caption store is not open")
        return self._pool.submit(fn, *args)

    def _connect(self, path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(path, check_same_thread=False)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
        except BaseException:
            # Windows cannot rename a file that still has an open handle, so a
            # corrupt store must be closed here before it is quarantined.
            conn.close()
            raise
        return conn

    @staticmethod
    def _probe(path: Path) -> bool:
        """Check an existing file without a writable handle or WAL.

        Closing a writable connection on a corrupt file can make SQLite remove
        the -wal sidecar, and Windows cannot rename a file that is still open.
        An immutable read-only probe touches neither, so the corrupt file and
        its sidecars can be moved aside intact.
        """
        try:
            if not path.exists() or path.stat().st_size == 0:
                return True
        except OSError:
            return True
        conn = None
        try:
            conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
            check = conn.execute("pragma quick_check").fetchone()
            return check is not None and str(check[0]).lower() == "ok"
        except sqlite3.Error:
            return False
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _quarantine(self, path: Path) -> None:
        stamp = time.strftime("%Y%m%d%H%M%S")
        dest = path.with_name(f"{path.name}.corrupt-{stamp}")
        extra = 0
        while dest.exists():
            extra += 1
            dest = path.with_name(f"{path.name}.corrupt-{stamp}-{extra}")
        path.rename(dest)
        for suffix in ("-wal", "-shm"):
            side = Path(str(path) + suffix)
            if side.exists():
                side.rename(dest.with_name(dest.name + suffix))
        self.quarantine_path = dest
        log.warning("caption store was unreadable; moved it to %s and started a new file", dest)

    def _open(self) -> None:
        self._writer = threading.current_thread()
        assert self.path is not None
        conn = None
        try:
            if not self._probe(self.path):
                raise sqlite3.DatabaseError("caption store failed a read-only check")
            conn = self._connect(self.path)
            check = conn.execute("pragma quick_check").fetchone()
            if check is None or str(check[0]).lower() != "ok":
                raise sqlite3.DatabaseError(f"quick_check {check}")
        except sqlite3.Error:
            log.warning("caption store at %s failed to open", self.path, exc_info=True)
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
            if self.path.exists():
                self._quarantine(self.path)
            self.recovered = True
            conn = self._connect(self.path)
        self._prepare(conn)
        self._conn = conn

    def _prepare(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            create table if not exists captions (
                id text primary key,
                room_id text not null,
                session_id text not null,
                seq integer not null,
                version integer not null,
                zh text,
                zh_raw text,
                en text,
                status text,
                t0_ms integer,
                t1_ms integer,
                updated_at real not null
            )
            """
        )
        columns = {row[1] for row in conn.execute("pragma table_info(captions)")}
        if "session_ord" not in columns:
            conn.execute("alter table captions add column session_ord integer")
        if "term_flags" not in columns:
            conn.execute("alter table captions add column term_flags text")
        conn.execute("create index if not exists captions_room_session_seq on captions (room_id, session_id, seq)")
        conn.execute("create index if not exists captions_updated_at on captions (updated_at)")
        # Per-room glossary. Not deleted by the caption TTL. Room reset deletes the row.
        conn.execute(
            """
            create table if not exists room_glossary (
                room_id text primary key,
                version integer not null,
                terms_json text not null,
                updated_at real not null
            )
            """
        )
        # Captions stay for host export. This marks the close, so the next open's replay can skip them.
        conn.execute(
            """
            create table if not exists room_replay_floor (
                room_id text primary key,
                floor_at real not null
            )
            """
        )
        conn.commit()
        self._conn = conn

    def _guard(self, fn, *args):
        try:
            return fn(*args)
        except Exception:
            self.errors += 1
            log.exception("caption store write failed")
            return None

    def submit_save(self, event: dict) -> None:
        if not self.enabled or not event.get("id") or self._closed:
            return
        self._submit(self._guard, self.save, dict(event))

    def save(self, event: dict) -> None:
        event = scrub_caption(event)
        if not self.enabled or not event.get("id"):
            return
        if self._on_writer():
            self._write_save(event)
            return
        self._submit(self.save, dict(event)).result()

    def _write_save(self, event: dict) -> None:
        conn = self._conn
        if conn is None:
            return
        with conn:
            conn.execute(
                """
                insert into captions (
                    id, room_id, session_id, seq, version, zh, zh_raw, en, status,
                    t0_ms, t1_ms, updated_at, session_ord, term_flags
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(id) do update set
                    version=excluded.version,
                    zh=excluded.zh,
                    zh_raw=excluded.zh_raw,
                    en=excluded.en,
                    status=excluded.status,
                    t0_ms=excluded.t0_ms,
                    t1_ms=excluded.t1_ms,
                    updated_at=excluded.updated_at,
                    session_ord=coalesce(excluded.session_ord, captions.session_ord),
                    term_flags=excluded.term_flags
                where excluded.version >= captions.version
                """,
                (
                    event.get("id"), event.get("room_id"), event.get("session_id"), int(event.get("seq") or 0),
                    int(event.get("version") or 1), event.get("zh") or "", event.get("zh_raw") or "",
                    event.get("en") or "", event.get("status") or "", event.get("t0_ms"), event.get("t1_ms"),
                    time.time(), event.get("session_ord"), _flags_text(event.get("term_flags")),
                ),
            )

    def enqueue_delete_room(self, room_id: str):
        """Queue the delete behind saves already submitted. Do not wait yet."""
        from concurrent.futures import Future

        if not self.enabled or self._pool is None:
            done: Future = Future()
            done.set_result(0)
            return done
        return self._pool.submit(self._guard_delete, self._delete_room_now, room_id)

    def enqueue_delete_id(self, room_id: str, seg_id: str):
        from concurrent.futures import Future

        if not self.enabled or self._pool is None:
            done: Future = Future()
            done.set_result(0)
            return done
        return self._pool.submit(self._guard_delete, self._delete_id_now, room_id, seg_id)

    def _guard_delete(self, fn, *args) -> int:
        try:
            deleted = fn(*args)
            return int(deleted or 0)
        except Exception:
            self.errors += 1
            log.exception("caption store delete failed")
            raise

    def delete_room(self, room_id: str) -> int:
        if not self.enabled:
            return 0
        if self._on_writer():
            return self._delete_room_now(room_id)
        deleted = self._submit(self.delete_room, room_id).result()
        return int(deleted or 0)

    def _delete_room_now(self, room_id: str) -> int:
        conn = self._conn
        if conn is None:
            return 0
        with conn:
            cur = conn.execute("delete from captions where room_id = ?", (room_id,))
            # Whole-room caption delete is the room reset. The glossary is not on the caption TTL.
            conn.execute("delete from room_glossary where room_id = ?", (room_id,))
            return int(cur.rowcount or 0)

    def delete_id(self, room_id: str, seg_id: str) -> int:
        if not self.enabled:
            return 0
        if self._on_writer():
            return self._delete_id_now(room_id, seg_id)
        deleted = self._submit(self.delete_id, room_id, seg_id).result()
        return int(deleted or 0)

    def _delete_id_now(self, room_id: str, seg_id: str) -> int:
        conn = self._conn
        if conn is None:
            return 0
        with conn:
            cur = conn.execute("delete from captions where room_id = ? and id = ?", (room_id, seg_id))
            return int(cur.rowcount or 0)

    def purge_expired(self, ttl_s: float) -> int:
        if not self.enabled:
            return 0
        if self._on_writer():
            return self._purge_now(ttl_s)
        removed = self._submit(self.purge_expired, ttl_s).result()
        return int(removed or 0)

    def _purge_now(self, ttl_s: float) -> int:
        conn = self._conn
        if conn is None:
            return 0
        cutoff = time.time() - ttl_s
        with conn:
            # room_glossary stays until the host resets or deletes the room.
            cur = conn.execute("delete from captions where updated_at < ?", (cutoff,))
            return int(cur.rowcount or 0)

    def has_id(self, room_id: str, seg_id: str) -> bool:
        if not self.enabled:
            return False
        if self._on_writer():
            return self._has_id_now(room_id, seg_id)
        return bool(self._submit(self.has_id, room_id, seg_id).result())

    def _has_id_now(self, room_id: str, seg_id: str) -> bool:
        conn = self._conn
        if conn is None:
            return False
        row = conn.execute(
            "select 1 from captions where room_id = ? and id = ? limit 1",
            (room_id, seg_id),
        ).fetchone()
        return row is not None

    def has_room(self, room_id: str) -> bool:
        if not self.enabled:
            return False
        if self._on_writer():
            return self._has_room_now(room_id)
        return bool(self._submit(self.has_room, room_id).result())

    def _has_room_now(self, room_id: str) -> bool:
        conn = self._conn
        if conn is None:
            return False
        row = conn.execute("select 1 from captions where room_id = ? limit 1", (room_id,)).fetchone()
        return row is not None

    def room_ids(self) -> list[str]:
        if not self.enabled:
            return []
        if self._on_writer():
            return self._room_ids_now()
        rows = self._submit(self.room_ids).result()
        return rows or []

    def _room_ids_now(self) -> list[str]:
        conn = self._conn
        if conn is None:
            return []
        found = conn.execute("select distinct room_id from captions").fetchall()
        return [str(row[0]) for row in found if row and row[0]]

    def room_rows(self, room_id: str) -> list[dict]:
        if not self.enabled:
            return []
        if self._on_writer():
            return self._room_rows_now(room_id)
        rows = self._submit(self.room_rows, room_id).result()
        return rows or []

    def _room_rows_now(self, room_id: str) -> list[dict]:
        conn = self._conn
        if conn is None:
            return []
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                with sessions as (
                    select session_id,
                           coalesce(min(nullif(session_ord, 0)), min(rowid)) as session_ord
                    from captions where room_id = ? group by session_id
                )
                select captions.id, captions.room_id, captions.session_id, captions.seq,
                       captions.version, captions.zh, captions.zh_raw, captions.en, captions.status,
                       captions.t0_ms, captions.t1_ms, captions.updated_at, sessions.session_ord,
                       captions.term_flags
                from captions join sessions on captions.session_id = sessions.session_id
                where captions.room_id = ?
                order by sessions.session_ord, captions.seq
                """,
                (room_id, room_id),
            ).fetchall()
            return [_caption_dict(row) for row in rows]
        finally:
            conn.row_factory = None

    def set_replay_floor(self, room_id: str, floor: float) -> None:
        if not self.enabled:
            return
        if self._on_writer():
            self._set_replay_floor_now(room_id, floor)
            return
        self._submit(self.set_replay_floor, room_id, float(floor)).result()

    def _set_replay_floor_now(self, room_id: str, floor: float) -> None:
        conn = self._conn
        if conn is None:
            return
        with conn:
            conn.execute(
                """
                insert into room_replay_floor (room_id, floor_at)
                values (?, ?)
                on conflict(room_id) do update set floor_at = excluded.floor_at
                where excluded.floor_at >= room_replay_floor.floor_at
                """,
                (room_id, float(floor)),
            )

    def get_replay_floor(self, room_id: str) -> float | None:
        if not self.enabled:
            return None
        if self._on_writer():
            return self._get_replay_floor_now(room_id)
        return self._submit(self.get_replay_floor, room_id).result()

    def _get_replay_floor_now(self, room_id: str) -> float | None:
        conn = self._conn
        if conn is None:
            return None
        row = conn.execute(
            "select floor_at from room_replay_floor where room_id = ?",
            (room_id,),
        ).fetchone()
        if not row or row[0] is None:
            return None
        try:
            return float(row[0])
        except (TypeError, ValueError):
            return None

    def save_glossary(
        self,
        room_id: str,
        version: int,
        terms: list,
        updated_at: float,
        expected_version: int | None = None,
    ) -> bool:
        """Write one glossary version. Return False when `expected_version` is not the stored one.

        Callers that omit `expected_version` mean "the version just before this one".
        A missing row is version 0, so the first save still inserts. A disabled store
        writes nothing and returns False; it is not a version conflict.
        """
        if not self.enabled:
            return False
        payload = json.dumps(list(terms or []), ensure_ascii=False, separators=(",", ":"))
        expected = int(version) - 1 if expected_version is None else int(expected_version)
        if self._on_writer():
            return self._write_glossary(room_id, int(version), payload, float(updated_at), expected)
        wrote = self._submit(
            self._write_glossary, room_id, int(version), payload, float(updated_at), expected
        ).result()
        return bool(wrote)

    def _write_glossary(
        self, room_id: str, version: int, payload: str, updated_at: float, expected_version: int
    ) -> bool:
        conn = self._conn
        if conn is None:
            raise RuntimeError("caption store is not open")
        with conn:
            row = conn.execute(
                "select version from room_glossary where room_id = ?",
                (room_id,),
            ).fetchone()
            # A deleted row is not version N. Inserting it again would bring the glossary back.
            current = int(row[0]) if row else 0
            if current != int(expected_version):
                return False
            before = conn.total_changes
            conn.execute(
                """
                insert into room_glossary (room_id, version, terms_json, updated_at)
                values (?, ?, ?, ?)
                on conflict(room_id) do update set
                    version=excluded.version,
                    terms_json=excluded.terms_json,
                    updated_at=excluded.updated_at
                where room_glossary.version = ?
                """,
                (room_id, int(version), payload, float(updated_at), int(expected_version)),
            )
            # total_changes stays put when the WHERE clause refuses the update.
            return conn.total_changes > before

    def delete_glossary(self, room_id: str) -> None:
        if not self.enabled:
            return
        if self._on_writer():
            self._delete_glossary_now(room_id)
            return
        self._submit(self._delete_glossary_now, room_id).result()

    def _delete_glossary_now(self, room_id: str) -> None:
        conn = self._conn
        if conn is None:
            return
        with conn:
            conn.execute("delete from room_glossary where room_id = ?", (room_id,))

    def get_glossary(self, room_id: str) -> dict | None:
        if not self.enabled:
            return None
        if self._on_writer():
            return self._get_glossary_now(room_id)
        return self._submit(self._get_glossary_now, room_id).result()

    def _get_glossary_now(self, room_id: str) -> dict | None:
        conn = self._conn
        if conn is None:
            return None
        row = conn.execute(
            "select room_id, version, terms_json, updated_at from room_glossary where room_id = ?",
            (room_id,),
        ).fetchone()
        return _glossary_row(row)

    def load_glossaries(self) -> list[dict]:
        if not self.enabled:
            return []
        if self._on_writer():
            return self._load_glossaries_now()
        rows = self._submit(self._load_glossaries_now).result()
        return rows or []

    def _load_glossaries_now(self) -> list[dict]:
        conn = self._conn
        if conn is None:
            return []
        found = conn.execute(
            "select room_id, version, terms_json, updated_at from room_glossary"
        ).fetchall()
        rows = []
        for row in found:
            parsed = _glossary_row(row)
            if parsed is not None:
                rows.append(parsed)
        return rows

    def flush(self) -> None:
        if not self.enabled or self._pool is None or self._closed:
            return
        if self._on_writer():
            return
        self._submit(lambda: None).result()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        pool = self._pool
        self._pool = None
        if pool is not None:
            pool.shutdown(wait=True)
        conn = self._conn
        self._conn = None
        if conn is not None:
            conn.close()
