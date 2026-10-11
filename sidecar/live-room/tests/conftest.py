import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _isolate_local_backend_env(monkeypatch):
    """Keep the suite off the developer's zen.sqlite3, Ollama, and VAD model.

    New local-backend features are opt-in (ledger, TM, local engine, VAD). Pin them off here
    even if the developer's shell sets them, so every test sees main's behaviour unless it
    opts in explicitly. No test may reach a model or the network.
    """
    monkeypatch.setenv("ZEN_LEDGER", "0")
    monkeypatch.setenv("BREEZE_TM", "0")
    monkeypatch.setenv("BREEZE_VAD", "off")
    monkeypatch.setenv("BREEZE_TRANSLATE_ENGINE", "openai")
    monkeypatch.setenv("BREEZE_ASR_LOG", "off")   # never write the developer's worker log
    monkeypatch.setenv("ZEN_DESKTOP_MAINT", "0")   # round3 C1: tests never touch the real data dir
    monkeypatch.setenv("ZEN_LOG", "off")          # CTO-07 file logs: only test_logfile turns them on
    for name in ("ZEN_DB_PATH", "ZEN_DATA_DIR", "BREEZE_TRANSLATE_BASE_URL", "BREEZE_TRANSLATE_ALLOW_REMOTE",
                 "BREEZE_TRANSLATE_MODEL", "BREEZE_TRANSLATE_EXTRA_BODY", "BREEZE_TRANSLATE_API_KEY",
                 "BREEZE_TRANSLATE_PROTOCOL", "BREEZE_TRANSLATE_NUM_THREAD", "BREEZE_TRANSLATE_NUM_CTX",
                 "BREEZE_TRANSLATE_KEEP_ALIVE", "BREEZE_TRANSLATE_STALE_S", "BREEZE_TRANSLATE_STALE_POLICY",
                 "BREEZE_TRANSLATE_PRIORITY", "BREEZE_TRANSLATE_QUEUE", "BREEZE_TRANSLATE_LATE_POLICY",
                 "BREEZE_TRANSLATE_LATE_S", "BREEZE_LOCKED_TERM_POLICY", "ZEN_EMBED", "ZEN_EMBED_IDLE_S",
                 "ZEN_ADMIN_TOKEN", "ZEN_ADMIN_HOST", "ZEN_ADMIN_PORT"):
        monkeypatch.delenv(name, raising=False)
