"""Caption-to-diagram generator ("字幕轉示意圖"), V2.

Ported from V1 (zen-bridge-visual-v1, commit 721b2f0, app/visual.py). V2 adds:

* ``strip_think``: ``<think>...</think>`` reasoning is removed from model output
  (also an unterminated ``<think>`` and a stray leading ``</think>``).
* De-duplication uses a *normalized* content hash (NFKC, case-folded, without
  whitespace / punctuation) and only within ``dedupe_window_ms`` (default 10 min).
* ``is_presentable``: drafts with no visible content are never broadcast.
* ``build_llm_from_env`` is local-only: a base URL that is not loopback disables
  the feature instead of sending captions off the machine.

The broadcast channel, the FastAPI router and the viewer page live in
``app/visual_routes.py``. This module still never opens a connection at import.
Everything here can be exercised in isolation:

* ``VisualTrigger`` decides *when* to make a diagram: every 30-60 s of final
  captions (measured with ``t0_ms`` / ``t1_ms``) or a manual host trigger, with a
  cooldown and de-duplication by ``source_ids`` and content hash.
* ``build_messages`` turns the last N captions (zh + en) plus glossary terms into
  a chat prompt.
* ``VisualLLM`` is the injected model interface (one async method). The default
  ``OpenAICompatLLM`` speaks OpenAI-compatible chat completions, but its base URL
  and model come only from settings / environment. With nothing configured the
  feature is disabled. No connection is made at import time.
* ``validate_draft`` checks the output against ``VISUAL_DRAFT_SCHEMA`` (a JSON
  Schema, validated by a small built-in checker so no new dependency is needed).
* ``sanitize_mermaid`` enforces the Mermaid safety rules; unsafe diagrams are
  downgraded to a ``concept_card``.
* On LLM timeout (default 15 s) or failure a ``concept_card`` containing only the
  glossary hits is produced with ``origin="fallback"``.
* ``VisualGenerator`` runs all of the above on its own asyncio task + queue (and
  the default LLM uses its own one-thread executor), so the caption /
  translation path only ever pays for a non-blocking ``feed()`` call.

``app.glossary`` is used read-only through its existing public functions
(``normalize``, ``matched_terms``, ``is_locked``). Glossary rows are obtained
from an injectable provider so this module never touches glossary storage.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import inspect
import json
import logging
import os
import re
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Mapping, Protocol, Sequence, Union, runtime_checkable

from . import glossary as _glossary

log = logging.getLogger("breeze.visual")

# ---------------------------------------------------------------------------
# Constants / schema
# ---------------------------------------------------------------------------

KINDS = ("concept_card", "mermaid_flow", "mermaid_mindmap", "compare_table")
MERMAID_KINDS = {"mermaid_flow": ("flowchart", "graph"), "mermaid_mindmap": ("mindmap",)}
MERMAID_WHITELIST = frozenset({"flowchart", "graph", "mindmap"})
MERMAID_MAX = 2000
# Matched case-insensitively as substrings. "%%{" also covers "%%{init".
MERMAID_FORBIDDEN = ("click", "href", "%%{init", "%%{", "callback", "<", "javascript:")
ORIGINS = ("llm", "fallback")
MAX_TERMS = 24
DEFAULT_TIMEOUT_S = 15.0

_TERM_SCHEMA = {
    "type": "object",
    "required": ["zh", "en", "locked"],
    "additionalProperties": False,
    "properties": {
        "zh": {"type": "string", "minLength": 1, "maxLength": 40},
        "en": {"type": "string", "maxLength": 120},
        "locked": {"type": "boolean"},
    },
}

VISUAL_DRAFT_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "breeze/visual_draft.v1",
    "title": "visual_draft",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "type", "id", "room_id", "version", "state", "kind", "source_ids", "t0_ms", "t1_ms",
        "title_zh", "title_en", "terms", "body_zh", "body_en", "mermaid", "pinned", "origin",
    ],
    "properties": {
        "type": {"const": "visual_draft"},
        "id": {"type": "string", "minLength": 1, "maxLength": 200},
        "room_id": {"type": "string", "minLength": 1, "maxLength": 120},
        "version": {"type": "integer", "minimum": 1},
        "state": {"const": "draft"},
        "kind": {"enum": list(KINDS)},
        "source_ids": {"type": "array", "minItems": 1, "maxItems": 200, "items": {"type": "string", "minLength": 1}},
        "t0_ms": {"type": "integer", "minimum": 0},
        "t1_ms": {"type": "integer", "minimum": 0},
        "title_zh": {"type": "string", "minLength": 1, "maxLength": 80},
        "title_en": {"type": "string", "maxLength": 160},
        "terms": {"type": "array", "maxItems": MAX_TERMS, "items": _TERM_SCHEMA},
        "body_zh": {"type": "string", "maxLength": 2000},
        "body_en": {"type": "string", "maxLength": 4000},
        "mermaid": {"type": "string", "maxLength": MERMAID_MAX},
        "pinned": {"const": False},
        "origin": {"enum": list(ORIGINS)},
    },
}


class SchemaError(ValueError):
    pass


_JSON_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def _check(value: Any, schema: Mapping, path: str, errors: list[str]) -> None:
    """Subset of JSON Schema 2020-12 used by VISUAL_DRAFT_SCHEMA."""
    if "const" in schema:
        expected = schema["const"]
        if type(value) is not type(expected) or value != expected:
            errors.append(f"{path}: must be {expected!r}")
            return
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: must be one of {schema['enum']}")
        return
    kind = schema.get("type")
    if kind is not None and not _JSON_TYPES[kind](value):
        errors.append(f"{path}: must be {kind}")
        return
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: too long")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and "minimum" in schema:
        if value < schema["minimum"]:
            errors.append(f"{path}: below minimum")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: too few items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: too many items")
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                _check(item, item_schema, f"{path}[{index}]", errors)
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: required")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    errors.append(f"{path}.{key}: not allowed")
        for key, sub in props.items():
            if key in value:
                _check(value[key], sub, f"{path}.{key}", errors)


def schema_errors(draft: Any) -> list[str]:
    errors: list[str] = []
    _check(draft, VISUAL_DRAFT_SCHEMA, "$", errors)
    if errors:
        return errors
    if draft["t1_ms"] < draft["t0_ms"]:
        errors.append("$.t1_ms: must be >= t0_ms")
    if draft["kind"] in MERMAID_KINDS:
        if not draft["mermaid"]:
            errors.append("$.mermaid: required for mermaid kinds")
        elif sanitize_mermaid(draft["mermaid"], draft["kind"]) is None:
            errors.append("$.mermaid: unsafe or wrong diagram type")
    elif draft["mermaid"]:
        errors.append("$.mermaid: must be empty for non-mermaid kinds")
    return errors


def validate_draft(draft: Any) -> dict:
    errors = schema_errors(draft)
    if errors:
        raise SchemaError("; ".join(errors[:8]))
    return draft


# ---------------------------------------------------------------------------
# Mermaid
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"^\s*```(?:mermaid)?\s*\n?(.*?)\n?```\s*$", re.S | re.I)


def sanitize_mermaid(source: Any, kind: str | None = None) -> str | None:
    """Return a safe Mermaid source, or None when it must be rejected."""
    if not isinstance(source, str):
        return None
    text = source.replace("\r\n", "\n").replace("\r", "\n")
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    text = text.strip()
    if not text or len(text) > MERMAID_MAX:
        return None
    lowered = text.lower()
    for bad in MERMAID_FORBIDDEN:
        if bad in lowered:
            return None
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in text):
        return None
    first = re.split(r"[\s;]+", text, maxsplit=1)[0]
    if first not in MERMAID_WHITELIST:
        return None
    if kind is not None:
        allowed = MERMAID_KINDS.get(kind)
        if allowed is None or first not in allowed:
            return None
    return text


# ---------------------------------------------------------------------------
# Config / LLM
# ---------------------------------------------------------------------------

ENV_BASE_URL = "BREEZE_VISUAL_LLM_BASE_URL"
ENV_MODEL = "BREEZE_VISUAL_LLM_MODEL"
ENV_API_KEY = "BREEZE_VISUAL_LLM_API_KEY"
ENV_TIMEOUT = "BREEZE_VISUAL_TIMEOUT"
ENV_MIN_WINDOW = "BREEZE_VISUAL_MIN_WINDOW_MS"
ENV_MAX_WINDOW = "BREEZE_VISUAL_MAX_WINDOW_MS"
ENV_COOLDOWN = "BREEZE_VISUAL_COOLDOWN_MS"
ENV_MAX_LINES = "BREEZE_VISUAL_MAX_LINES"
ENV_DEDUPE_WINDOW = "BREEZE_VISUAL_DEDUPE_WINDOW_MS"


def _num(env: Mapping[str, str], key: str, default, cast):
    raw = env.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = cast(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class VisualConfig:
    base_url: str = ""
    model: str = ""
    timeout_s: float = DEFAULT_TIMEOUT_S
    min_window_ms: int = 30_000
    max_window_ms: int = 60_000
    cooldown_ms: int = 30_000
    max_lines: int = 12
    queue_size: int = 2
    dedupe_memory: int = 64
    dedupe_window_ms: int = 600_000

    @property
    def llm_configured(self) -> bool:
        return bool(self.base_url and self.model)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "VisualConfig":
        env = os.environ if env is None else env
        min_window = _num(env, ENV_MIN_WINDOW, 30_000, int)
        max_window = max(_num(env, ENV_MAX_WINDOW, 60_000, int), min_window)
        return cls(
            base_url=str(env.get(ENV_BASE_URL) or "").strip(),
            model=str(env.get(ENV_MODEL) or "").strip(),
            timeout_s=_num(env, ENV_TIMEOUT, DEFAULT_TIMEOUT_S, float),
            min_window_ms=min_window,
            max_window_ms=max_window,
            cooldown_ms=_num(env, ENV_COOLDOWN, 30_000, int),
            max_lines=_num(env, ENV_MAX_LINES, 12, int),
            dedupe_window_ms=_num(env, ENV_DEDUPE_WINDOW, 600_000, int),
        )


@runtime_checkable
class VisualLLM(Protocol):
    """Injected model. Must return the assistant text (expected to be JSON)."""

    async def complete(self, messages: list[dict], *, timeout_s: float) -> str: ...


class OpenAICompatLLM:
    """OpenAI-compatible ``/chat/completions`` client.

    Nothing is opened at construction. Each call runs the blocking HTTP request
    on this client's own single-thread executor, never on the event loop and
    never on the translation workers.
    """

    def __init__(self, base_url: str, model: str, api_key: str | None = None):
        if not base_url or not model:
            raise ValueError("base_url and model are required")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key or None
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None

    def _pool(self) -> concurrent.futures.ThreadPoolExecutor:
        if self._executor is None:
            self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="visual-llm")
        return self._executor

    def _post(self, payload: dict, timeout_s: float) -> dict:
        import urllib.request  # local import: nothing network-related at module import

        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        if self._api_key:
            request.add_header("Authorization", "Bearer " + self._api_key)
        if not is_loopback_url(self.base_url):          # CTO gate P1: re-check at call time (never 8645)
            raise ValueError("visual LLM base URL is not an allowed local URL")
        from app.net import safe_opener                  # no redirects, no proxy env
        with safe_opener()(request, timeout=timeout_s) as response:  # noqa: S310 - configured URL only
            return json.loads(response.read().decode("utf-8"))

    async def complete(self, messages: list[dict], *, timeout_s: float) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(self._pool(), self._post, payload, timeout_s)
        return str(data["choices"][0]["message"]["content"] or "")

    def close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def is_loopback_url(url: str) -> bool:
    """True only for http(s) URLs whose host is this machine."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(str(url or "").strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    if parts.scheme not in {"http", "https"}:
        return False
    try:
        from app.net import effective_port
        from app.translate_config import RESERVED_PORTS
        if effective_port(parts) in RESERVED_PORTS:      # CTO gate P1: 8645 is Hermes, never called
            return False
    except ValueError:
        return False
    if parts.username is not None or parts.password is not None:
        return False
    if host in LOOPBACK_HOSTS:
        return True
    import ipaddress
    try:
        return ipaddress.ip_address(host).is_loopback          # 127.0.0.2 yes, 127.evil.example no
    except ValueError:
        return False


def build_llm_from_env(env: Mapping[str, str] | None = None, config: VisualConfig | None = None) -> OpenAICompatLLM | None:
    """Default LLM, or None (feature disabled).

    Disabled when base URL / model are not set, and also when the base URL is not
    loopback: the default backend is local-only (e.g. a local Ollama / llama.cpp
    OpenAI-compatible server). Captions never leave the machine by default.
    """
    env = os.environ if env is None else env
    config = config or VisualConfig.from_env(env)
    if not config.llm_configured:
        return None
    if not is_loopback_url(config.base_url):
        log.warning("visual: base URL is not loopback; visual drafts stay disabled (local-only)")
        return None
    return OpenAICompatLLM(config.base_url, config.model, str(env.get(ENV_API_KEY) or "") or None)


# ---------------------------------------------------------------------------
# Captions / trigger
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CaptionLine:
    id: str
    zh: str
    en: str
    t0_ms: int
    t1_ms: int


def caption_from_event(event: Mapping) -> CaptionLine | None:
    """Accept a pipeline ``final`` caption event (Segment.public()); ignore others."""
    if not isinstance(event, Mapping) or event.get("type") != "final":
        return None
    zh = str(event.get("zh") or "").strip()
    cid = str(event.get("id") or "").strip()
    t0, t1 = event.get("t0_ms"), event.get("t1_ms")
    if not zh or not cid:
        return None
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (t0, t1)):
        return None
    if t0 < 0 or t1 < t0:
        return None
    return CaptionLine(cid, zh, str(event.get("en") or "").strip(), t0, t1)


@dataclass(frozen=True)
class VisualJob:
    room_id: str
    lines: tuple[CaptionLine, ...]
    manual: bool
    key: str

    @property
    def source_ids(self) -> list[str]:
        return [line.id for line in self.lines]

    @property
    def t0_ms(self) -> int:
        return min(line.t0_ms for line in self.lines)

    @property
    def t1_ms(self) -> int:
        return max(line.t1_ms for line in self.lines)


def normalize_for_hash(text: str) -> str:
    """NFKC + casefold, without whitespace, punctuation or symbols."""
    folded = unicodedata.normalize("NFKC", text or "").casefold()
    return "".join(ch for ch in folded if unicodedata.category(ch)[0] not in {"Z", "P", "S", "C"})


def _content_hash(lines: Iterable[CaptionLine]) -> str:
    joined = "\n".join(normalize_for_hash(line.zh) for line in lines)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def _ids_hash(lines: Iterable[CaptionLine]) -> str:
    joined = "\n".join(sorted(line.id for line in lines))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


class VisualTrigger:
    """Pure, synchronous beat / cooldown / de-dupe logic. ``clock`` returns ms."""

    def __init__(self, room_id: str, config: VisualConfig | None = None, clock: Callable[[], float] | None = None):
        self.room_id = room_id
        self.config = config or VisualConfig()
        self._clock = clock or (lambda: time.monotonic() * 1000.0)
        self._pending: list[CaptionLine] = []
        self._recent: deque[CaptionLine] = deque(maxlen=self.config.max_lines)
        self._last_fire: float | None = None
        # key -> fire time (ms). Bounded by dedupe_memory and expired after dedupe_window_ms.
        self._seen_ids: dict[str, float] = {}
        self._seen_content: dict[str, float] = {}
        self.skipped: dict[str, int] = {"cooldown": 0, "duplicate": 0, "empty": 0}

    @property
    def pending_span_ms(self) -> int:
        if not self._pending:
            return 0
        return self._pending[-1].t1_ms - self._pending[0].t0_ms

    def in_cooldown(self) -> bool:
        return self._last_fire is not None and self._clock() - self._last_fire < self.config.cooldown_ms

    def _upsert(self, bucket, line: CaptionLine) -> bool:
        for index, old in enumerate(bucket):
            if old.id == line.id:
                bucket[index] = line
                return True
        return False

    def add(self, line: CaptionLine) -> VisualJob | None:
        if not self._upsert(self._recent, line):
            self._recent.append(line)
        if not self._upsert(self._pending, line):
            if self._pending and line.t1_ms < self._pending[0].t0_ms:
                self._pending.clear()  # clock went backwards (new session): restart the window
            self._pending.append(line)
        # keep the window at most max_window_ms
        while len(self._pending) > 1 and self.pending_span_ms > self.config.max_window_ms:
            self._pending.pop(0)
        if self.pending_span_ms < self.config.min_window_ms:
            return None
        if self.in_cooldown():
            self.skipped["cooldown"] += 1
            return None
        return self._fire(tuple(self._pending[-self.config.max_lines:]), manual=False)

    def manual(self, force: bool = False) -> VisualJob | None:
        lines = tuple(self._recent)
        if not lines:
            self.skipped["empty"] += 1
            return None
        if not force and self.in_cooldown():
            self.skipped["cooldown"] += 1
            return None
        return self._fire(lines, manual=True, force=force)

    def _expire_seen(self, now: float) -> None:
        window = self.config.dedupe_window_ms
        for seen in (self._seen_ids, self._seen_content):
            for key in [k for k, at in seen.items() if now - at >= window]:
                del seen[key]
            while len(seen) > self.config.dedupe_memory:
                del seen[next(iter(seen))]

    def _remember(self, seen: dict[str, float], key: str, now: float) -> None:
        seen.pop(key, None)
        seen[key] = now
        while len(seen) > self.config.dedupe_memory:
            del seen[next(iter(seen))]

    def _fire(self, lines: tuple[CaptionLine, ...], manual: bool, force: bool = False) -> VisualJob | None:
        ids_key = _ids_hash(lines)
        content_key = _content_hash(lines)
        now = self._clock()
        self._expire_seen(now)
        if not force and (ids_key in self._seen_ids or content_key in self._seen_content):
            self.skipped["duplicate"] += 1
            self._pending.clear()
            return None
        self._remember(self._seen_ids, ids_key, now)
        self._remember(self._seen_content, content_key, now)
        self._last_fire = now
        self._pending.clear()
        return VisualJob(self.room_id, lines, manual, ids_key + ":" + content_key)


# ---------------------------------------------------------------------------
# Glossary (read-only)
# ---------------------------------------------------------------------------

GlossaryProvider = Callable[[str], Union[Sequence[Mapping], Awaitable[Sequence[Mapping]]]]


def glossary_hits(lines: Sequence[CaptionLine], rows: Sequence[Mapping] | None) -> list[dict]:
    """Glossary terms that appear in the captions, locked ones first.

    Uses only public helpers of app.glossary; ``en`` always comes from the glossary.
    """
    rows = list(rows or [])
    if not rows:
        return []
    picked: dict[str, dict] = {}
    for line in lines:
        text = _glossary.normalize(line.zh, rows)
        for term in _glossary.matched_terms(text, rows):
            zh = term["zh"]
            en = str(term.get("en") or "").strip()
            if zh in picked or not en:
                continue
            picked[zh] = {"zh": zh, "en": en, "locked": bool(_glossary.is_locked(term))}
    ordered = sorted(picked.values(), key=lambda t: (not t["locked"],))
    return ordered[:MAX_TERMS]


def merge_terms(hits: Sequence[dict], llm_terms: Any) -> list[dict]:
    """Glossary hits always win; LLM extras are kept only when they do not clash."""
    merged: dict[str, dict] = {t["zh"]: dict(t) for t in hits}
    if isinstance(llm_terms, list):
        for item in llm_terms:
            if not isinstance(item, Mapping):
                continue
            zh = strip_think(item.get("zh"))[:40]
            en = strip_think(item.get("en"))[:120]
            if not zh or zh in merged:
                continue
            merged[zh] = {"zh": zh, "en": en, "locked": False}
    return list(merged.values())[:MAX_TERMS]


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "你是即時講座的示意圖助理。根據字幕片段，產出一個簡潔的示意圖草稿，只輸出一個 JSON 物件，"
    "欄位：kind（concept_card | mermaid_flow | mermaid_mindmap | compare_table）、title_zh、title_en、"
    "body_zh、body_en、mermaid、terms（[{zh,en}]）。"
    "mermaid 只在 kind 為 mermaid_flow（以 flowchart 或 graph 開頭）或 mermaid_mindmap（以 mindmap 開頭）時填寫，"
    "最多 2000 字元，不得使用 click、href、%%{init}、HTML 標籤或連結；其他 kind 的 mermaid 留空字串。"
    "compare_table 用 Markdown 表格寫在 body_zh／body_en。"
    "術語表給定的英文用詞必須原樣使用。不要加入字幕沒有提到的內容。"
)


def build_messages(lines: Sequence[CaptionLine], terms: Sequence[dict], max_lines: int = 12) -> list[dict]:
    recent = list(lines)[-max_lines:]
    caption_block = "\n".join(
        f"{index + 1}. 中：{line.zh}\n   EN: {line.en or '(no translation)'}" for index, line in enumerate(recent)
    )
    term_block = "\n".join(
        f"- {t['zh']} = {t['en']}{'（鎖定）' if t.get('locked') else ''}" for t in terms
    ) or "（無）"
    user = f"字幕（舊到新）：\n{caption_block}\n\n術語表（英文必須一致）：\n{term_block}\n\n請輸出 JSON。"
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


# ---------------------------------------------------------------------------
# Draft assembly
# ---------------------------------------------------------------------------

_THINK_BLOCK = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.S | re.I)
_THINK_OPEN = re.compile(r"<think\b[^>]*>", re.I)
_THINK_CLOSE = re.compile(r"</think\s*>", re.I)


def strip_think(text: Any) -> str:
    """Remove reasoning blocks. An unterminated ``<think>`` drops everything after it;
    a stray ``</think>`` drops everything before it (some models omit the opener)."""
    raw = str(text or "")
    raw = _THINK_BLOCK.sub("", raw)
    closes = list(_THINK_CLOSE.finditer(raw))
    if closes:
        raw = raw[closes[-1].end():]
    opened = _THINK_OPEN.search(raw)
    if opened:
        raw = raw[:opened.start()]
    return raw.strip()


def _parse_json_object(text: str) -> dict:
    raw = strip_think(text)
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", raw, re.S | re.I)
    if fenced:
        raw = fenced.group(1)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise
        data = json.loads(raw[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("LLM output is not a JSON object")
    return data


def _text(value: Any, limit: int) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    return strip_think(value)[:limit]


def _base(job: VisualJob, version: int, draft_id: str, kind: str, origin: str) -> dict:
    return {
        "type": "visual_draft",
        "id": draft_id,
        "room_id": job.room_id,
        "version": version,
        "state": "draft",
        "kind": kind,
        "source_ids": job.source_ids,
        "t0_ms": job.t0_ms,
        "t1_ms": job.t1_ms,
        "title_zh": "",
        "title_en": "",
        "terms": [],
        "body_zh": "",
        "body_en": "",
        "mermaid": "",
        "pinned": False,
        "origin": origin,
    }


def fallback_draft(job: VisualJob, hits: Sequence[dict], version: int, draft_id: str) -> dict:
    """concept_card made only from glossary hits. Always schema-valid."""
    draft = _base(job, version, draft_id, "concept_card", "fallback")
    terms = [dict(t) for t in hits][:MAX_TERMS]
    draft["terms"] = terms
    draft["title_zh"] = "本段術語"
    draft["title_en"] = "Key terms"
    draft["body_zh"] = "、".join(t["zh"] for t in terms)[:2000]
    draft["body_en"] = ", ".join(t["en"] for t in terms)[:4000]
    return validate_draft(draft)


def draft_from_llm(job: VisualJob, data: Mapping, hits: Sequence[dict], version: int, draft_id: str) -> dict:
    """Server-controlled fields are never taken from the model."""
    kind = data.get("kind") if data.get("kind") in KINDS else "concept_card"
    draft = _base(job, version, draft_id, kind, "llm")
    draft["title_zh"] = _text(data.get("title_zh"), 80) or "示意圖"
    draft["title_en"] = _text(data.get("title_en"), 160)
    draft["body_zh"] = _text(data.get("body_zh"), 2000)
    draft["body_en"] = _text(data.get("body_en"), 4000)
    draft["terms"] = merge_terms(hits, data.get("terms"))
    if kind in MERMAID_KINDS:
        safe = sanitize_mermaid(data.get("mermaid"), kind)
        if safe is None:
            log.info("visual: mermaid rejected, downgraded to concept_card")
            draft["kind"] = "concept_card"
            draft["mermaid"] = ""
        else:
            draft["mermaid"] = safe
    return validate_draft(draft)


def is_presentable(draft: Any) -> bool:
    """A draft worth showing: schema-valid and with real content.

    Rejects the empty fallback card (no glossary hits), placeholder-only titles
    with no body / terms / diagram, and anything still carrying think tags.
    """
    if schema_errors(draft):
        return False
    visible = [draft["body_zh"], draft["body_en"], draft["mermaid"]] + [t["zh"] for t in draft["terms"]]
    if not any(normalize_for_hash(part) for part in visible):
        return False
    texts = [draft["title_zh"], draft["title_en"], draft["body_zh"], draft["body_en"], draft["mermaid"]]
    if any("<think" in part.lower() or "</think" in part.lower() for part in texts):
        return False
    return True


async def generate_draft(
    job: VisualJob,
    llm: VisualLLM,
    hits: Sequence[dict],
    *,
    version: int,
    draft_id: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_lines: int = 12,
) -> dict:
    messages = build_messages(job.lines, hits, max_lines)
    try:
        text = await asyncio.wait_for(llm.complete(messages, timeout_s=timeout_s), timeout=timeout_s)
        return draft_from_llm(job, _parse_json_object(text), hits, version, draft_id)
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        log.warning("visual: LLM timed out after %.1fs, using fallback", timeout_s)
    except Exception as exc:  # LLM error, bad JSON, schema failure
        log.warning("visual: LLM draft failed (%s), using fallback", type(exc).__name__)
    return fallback_draft(job, hits, version, draft_id)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

DraftSink = Callable[[dict], Union[None, Awaitable[None]]]


class VisualGenerator:
    """Per-room runner with its own asyncio task and bounded queue.

    ``feed`` / ``trigger`` are synchronous and cheap: they only update the
    trigger state and ``put_nowait`` a job. All LLM work happens on the worker
    task. With ``llm=None`` the generator is disabled and ignores input.
    """

    def __init__(
        self,
        room_id: str,
        llm: VisualLLM | None,
        *,
        config: VisualConfig | None = None,
        glossary_provider: GlossaryProvider | None = None,
        on_draft: DraftSink | None = None,
        clock: Callable[[], float] | None = None,
    ):
        self.room_id = room_id
        self.llm = llm
        self.config = config or VisualConfig()
        self.trigger_state = VisualTrigger(room_id, self.config, clock)
        self._glossary_provider = glossary_provider
        self._on_draft = on_draft
        self._queue: asyncio.Queue[VisualJob] | None = None
        self._task: asyncio.Task | None = None
        self._version = 0
        self.drafts: list[dict] = []
        self.dropped = 0

    @property
    def enabled(self) -> bool:
        return self.llm is not None

    def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        self._queue = asyncio.Queue(maxsize=self.config.queue_size)
        self._task = asyncio.get_running_loop().create_task(self._worker(), name=f"visual:{self.room_id}")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def drain(self) -> None:
        if self._queue is not None:
            await self._queue.join()

    def _enqueue(self, job: VisualJob | None) -> bool:
        if job is None or self._queue is None:
            return False
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            self.dropped += 1
            log.info("visual: queue full, dropping job for %s", self.room_id)
            return False
        return True

    def feed(self, event: Mapping) -> bool:
        if not self.enabled:
            return False
        line = caption_from_event(event)
        if line is None:
            return False
        return self._enqueue(self.trigger_state.add(line))

    def trigger(self, force: bool = False) -> bool:
        """Manual host trigger."""
        if not self.enabled:
            return False
        return self._enqueue(self.trigger_state.manual(force=force))

    async def _rows(self) -> list:
        if self._glossary_provider is None:
            return []
        try:
            rows = self._glossary_provider(self.room_id)
            if inspect.isawaitable(rows):
                rows = await rows
            return list(rows or [])
        except Exception as exc:
            log.warning("visual: glossary provider failed (%s)", type(exc).__name__)
            return []

    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            job = await self._queue.get()
            try:
                hits = glossary_hits(job.lines, await self._rows())
                self._version += 1
                draft_id = f"visual:{self.room_id}:{self._version}"
                draft = await generate_draft(
                    job, self.llm, hits,
                    version=self._version, draft_id=draft_id,
                    timeout_s=self.config.timeout_s, max_lines=self.config.max_lines,
                )
                self.drafts.append(draft)
                if self._on_draft is not None:
                    result = self._on_draft(draft)
                    if inspect.isawaitable(result):
                        await result
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("visual: worker error")
            finally:
                self._queue.task_done()
