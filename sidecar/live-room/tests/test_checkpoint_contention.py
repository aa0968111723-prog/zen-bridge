import sqlite3
import time
from pathlib import Path
from app.admin.db import try_checkpoint
from app.ledger import Ledger

def test_checkpoint_returns_while_reader_is_pinned_and_restores_write_timeout(tmp_path):
    path=tmp_path/'live.sqlite3'
    writer=sqlite3.connect(path,isolation_level=None)
    reader=sqlite3.connect(path,isolation_level=None)
    try:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA busy_timeout=5000')
        writer.execute('CREATE TABLE captions(text TEXT)')
        reader.execute('BEGIN')
        assert reader.execute('SELECT count(*) FROM captions').fetchone()[0]==0
        writer.execute("INSERT INTO captions VALUES ('preserved caption')")
        started=time.monotonic()
        result=try_checkpoint(writer)
        assert time.monotonic()-started<0.5
        assert result[0]==1
        assert writer.execute('PRAGMA busy_timeout').fetchone()[0]==5000
        assert reader.in_transaction
        assert writer.execute('SELECT count(*) FROM captions').fetchone()[0]==1
        reader.execute('COMMIT')
        assert tuple(try_checkpoint(writer))==(0,0,0)
        assert writer.execute('PRAGMA busy_timeout').fetchone()[0]==5000
        assert writer.execute('SELECT text FROM captions').fetchone()[0]=='preserved caption'
    finally:
        reader.close();writer.close()


def test_busy_ledger_checkpoint_remains_due_until_reader_releases(tmp_path):
    path = tmp_path / 'retry.sqlite3'
    writer = sqlite3.connect(path, isolation_level=None)
    reader = sqlite3.connect(path, isolation_level=None)
    ledger = Ledger(path)
    try:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE captions(text TEXT)')
        reader.execute('BEGIN')
        reader.execute('SELECT count(*) FROM captions').fetchone()
        writer.execute("INSERT INTO captions VALUES ('durable')")
        ledger._maybe_checkpoint(writer, ledger.CHECKPOINT_EVERY)
        assert ledger._since_ckpt >= ledger.CHECKPOINT_EVERY
        assert ledger._checkpoint_busy
        reader.execute('COMMIT')
        # No additional 300-event wait: the very next batch uses this gap.
        ledger._maybe_checkpoint(writer, 1)
        assert ledger._since_ckpt == 0 and not ledger._checkpoint_busy
        assert Path(str(path) + '-wal').stat().st_size == 0
        assert writer.execute('SELECT text FROM captions').fetchone()[0] == 'durable'
    finally:
        reader.close(); writer.close()
