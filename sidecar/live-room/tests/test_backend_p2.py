"""後端 B7 (cross-site cookie GET / SSE refused), P2-b (cancel a finished job -> 409),
P2-d (Idempotency-Key is scoped to the caller)."""
from tests.test_admin_api import API, BEARER, ORIGIN, browser_login, env, writes  # noqa: F401


def test_cross_site_api_get_refused(env):  # noqa: F811
    c = env["client"]
    browser_login(c, env["app"])
    assert c.get(f"{API}/sessions", headers={"sec-fetch-site": "same-site"}).status_code == 403
    assert c.get(f"{API}/live/stream", headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert c.get(f"{API}/sessions", headers={"sec-fetch-site": "same-origin"}).status_code == 200
    assert c.get(f"{API}/sessions", headers=BEARER).status_code == 200          # CLI: no fetch metadata


def test_cancel_finished_job_is_409(env):  # noqa: F811
    c = env["client"]
    job, _ = env["app"].state.store.create("backup", {}, {})
    env["app"].state.store.finish(job["id"], "succeeded", result={})
    r = c.post(f"{API}/jobs/{job['id']}/cancel", headers=BEARER)
    assert r.status_code == 409 and r.json()["code"] == "job_finished"
    assert c.post(f"{API}/jobs/job_nope/cancel", headers=BEARER).status_code == 404


def test_idempotency_key_is_per_caller(env):  # noqa: F811
    c = env["client"]
    body = {"segment_id": "s1-1", "target_type": "translation", "text": "Today, conditions ripen."}
    r1 = c.post(f"{API}/corrections", json=body, headers={**BEARER, "idempotency-key": "same-key-1"})
    assert r1.status_code == 201, r1.text
    csrf = browser_login(c, env["app"])
    r2 = c.post(f"{API}/corrections", json=body, headers={**writes(csrf), "idempotency-key": "same-key-1"})
    assert r2.status_code == 201 and r2.json().get("correction_id") != r1.json().get("correction_id"), (r1.json(), r2.json())
