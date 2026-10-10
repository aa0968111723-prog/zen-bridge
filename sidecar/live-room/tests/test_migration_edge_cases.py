import os
import sqlite3

import pytest

from app.admin import db, migrate
from tests.local_fakes import migrated_db
from tests.test_migrations_v3 import v2_db


def test_migrate_cli_upgrades_v2_once_and_keeps_pre_migrate_snapshot(tmp_path, capsys):
    path = v2_db(tmp_path / "zen.sqlite3")
    snapshot = tmp_path / "pre-migrate-v2.sqlite3"

    assert migrate.main(["--db", str(path)]) == 0
    assert db.SCHEMA_VERSION == 12
    with sqlite3.connect(path) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 12
    assert snapshot.is_file()
    with sqlite3.connect(snapshot) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2

    assert migrate.main(["--db", str(path)]) == 0
    assert sorted(p.name for p in tmp_path.glob("pre-migrate-*")) == ["pre-migrate-v2.sqlite3"]
    assert "schema v12 OK" in capsys.readouterr().out


def test_migrate_cli_reports_step_checksum_mismatch(tmp_path, capsys):
    path = migrated_db(tmp_path)
    conn = db.connect(path)
    try:
        conn.execute("UPDATE schema_migrations SET checksum=? WHERE version=3", ("0" * 64,))
    finally:
        conn.close()

    assert migrate.main(["--db", str(path)]) == 2
    assert "0003_glossary_targets.sql" in capsys.readouterr().err


def test_localappdata_with_spaces_and_non_ascii_migrates(tmp_path):
    local = tmp_path / "Local AppData-日本語"
    path = db.default_db_path({"LOCALAPPDATA": str(local)})

    assert path == local / "ZenBridge" / "data" / "zen.sqlite3"
    assert db.migrate(path) == db.SCHEMA_VERSION
    assert path.is_file()


@pytest.mark.skipif(os.name != "nt", reason="Exercises Windows filesystem long-path handling.")
def test_windows_long_localappdata_path_can_migrate(tmp_path):
    local = tmp_path
    for index in range(4):
        local = local / (f"segment-{index}-" + "x" * 65)
    # The bundled interpreter must also work when OS LongPathsEnabled is off.
    # Prepare the fixture with the Win32 prefix; the product normalizes the
    # unprefixed LOCALAPPDATA input itself.
    from pathlib import Path
    Path('\\\\?\\' + str(local.resolve())).mkdir(parents=True)
    path = db.default_db_path({"LOCALAPPDATA": str(local)})

    assert len(str(path)) > 260
    assert db.migrate(path) == db.SCHEMA_VERSION
    assert path.is_file()
