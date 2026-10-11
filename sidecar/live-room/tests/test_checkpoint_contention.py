import sqlite3
import time
from app.admin.db import try_checkpoint

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
