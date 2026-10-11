"""QA 後端 B6: glossary push only sends the diff the user saw (if_room_version), compares aliases,
never silently overwrites a host-locked term, and validates before calling live."""
import pytest

from app.admin import db
from tests.test_admin_api import BEARER, API, env  # noqa: F401  (fixture)

URL = f"{API}/rooms/class/glossary/push"


def seed(env, *rows):
    c = db.connect(env["path"])
    c.execute("INSERT INTO glossaries(id, name, scope) VALUES (1, 'g', 'global')")
    for zh, en, aliases in rows:
        c.execute("INSERT INTO glossary_terms(glossary_id, zh, en, aliases, status) VALUES (1, ?, ?, ?, 'active')",
                  (zh, en, aliases))
    c.close()


def test_push_requires_if_room_version(env):
    seed(env, ("因緣", "conditions", "[]"))
    r = env["client"].post(URL, json={}, headers=BEARER)
    assert r.status_code == 428 and r.json()["code"] == "precondition_required"
    assert env["live"].puts == []


def test_stale_view_is_409_without_calling_live(env):
    seed(env, ("因緣", "conditions", "[]"))
    assert env["client"].post(URL, json={"dry_run": True}, headers=BEARER).json()["version"] == 3
    env["live"].room["version"] = 4                       # host edited in between
    r = env["client"].post(URL, json={"if_room_version": 3}, headers=BEARER)
    assert r.status_code == 409 and env["live"].puts == []


def test_alias_change_is_a_change(env):
    seed(env, ("禪修", "meditation", '["禪坐修行"]'))
    d = env["client"].post(URL, json={"dry_run": True}, headers=BEARER).json()
    assert d["changed"] and d["updated"][0]["aliases_to"] == ["禪坐修行"]


def test_host_locked_term_not_overwritten_unless_forced(env):
    seed(env, ("禪修", "Chan practice", "[]"))
    env["live"].room["terms"][0]["locked"] = True
    d = env["client"].post(URL, json={"dry_run": True}, headers=BEARER).json()
    assert d["locked_conflicts"] and not d["updated"] and not d["changed"]
    d = env["client"].post(URL, json={"dry_run": True, "force_locked": True}, headers=BEARER).json()
    assert d["updated"] and d["changed"]


def test_invalid_terms_422_before_live(env):
    seed(env, ("因緣", "conditions", '["因緣"]'))          # alias equal to its own canonical term
    r = env["client"].post(URL, json={"if_room_version": 3}, headers=BEARER)
    assert r.status_code == 422 and r.json()["code"] == "glossary_invalid" and env["live"].puts == []
