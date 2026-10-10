"""Translation backend interface + Hy-MT2-1.8B (llama-server) stub — optimization-round2 #11/#13.

NOT wired into the pipeline yet (18:30 work). What exists here:

* ``TARGET_LANGS = ("en", "ja")``: one target language per session, chosen at session start.
* ``SessionTargets``: per-(room, session) target language. A change mid-session applies from
  the NEXT segment (``lang_for(seq)``), never to a line already queued; each change is recorded
  so the caller can write ``session_events(kind='tgt_lang_changed')``.
* ``MtBackend`` protocol: ``translate(zh, *, tgt_lang, glossary, context, deadline, cancel)``
  returning the existing ``TranslateResult`` (so pipeline/TM/ledger code keeps working).
* ``HyMtLlamaServer``: llama-server OpenAI-compatible ``/v1/chat/completions`` client for
  Hy-MT2-1.8B (Q4_K_M). Same model for en and ja; switching only changes the prompt.
  Loopback only, 8645 refused, no redirects / env proxy (app.net.safe_opener).
  The prompt template is configurable (``BREEZE_MT_PROMPT``) because the exact Hy-MT2 chat
  template is UNVERIFIED here; the default follows the Hunyuan-MT model-card style.
* ``validate_caption(text, tgt_lang)``: en uses the existing plain-English gate; ja has its
  own gate (kana/kanji allowed, no think/template tokens, one line, length cap).

Env (all optional): BREEZE_MT_BACKEND=hymt|off (default off), BREEZE_MT_BASE_URL
(default http://127.0.0.1:8081/v1), BREEZE_MT_MODEL (default hy-mt2-1.8b), BREEZE_MT_PROMPT,
BREEZE_MT_TEMPERATURE (0.2), BREEZE_MT_MAX_TOKENS (256), BREEZE_DEFAULT_TGT_LANG (en).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

from app.translate import TranslateResult, validate_caption_en

TARGET_LANGS = ("en", "ja")
LANG_NAMES = {"en": "英语", "ja": "日语"}
# round3 C6: Hy-MT2-1.8B model-card templates, verbatim (Chinese prompts, Chinese language names).
DEFAULT_PROMPT = ("将以下文本翻译为{target_language}，注意只需要输出翻译后的结果，不要额外解释：\n\n{source_text}")
TERMS_HEADER = "参考下面的翻译：\n"
TERM_LINE = "{src} 翻译成 {tgt}"
CONTEXT_PROMPT = ("【背景信息】\n{background_text}\n\n请结合背景信息将以下文本翻译为{target_language}。"
                  "\n\n【待翻译文本】\n{source_text}")          # card: "Structured Data 2"
CONTEXT_LINES = 2
DEFAULT_BASE = "http://127.0.0.1:8081/v1"


class TargetLangError(ValueError):
    pass


def validate_tgt_lang(lang: str | None) -> str:
    v = (lang or "").strip().lower()
    if v not in TARGET_LANGS:
        raise TargetLangError("目標語只能是 en 或 ja")
    return v


# ---------------------------------------------------------------- per-session target language
@dataclass
class _SessionLang:
    lang: str
    from_seq: int = 0
    history: list = field(default_factory=list)     # [(from_seq, lang, at)]


class SessionTargets:
    """One target language per session; a switch only applies from the next segment."""

    def __init__(self, default: str = "en", clock=time.time):
        self.default = validate_tgt_lang(default)
        self._clock = clock
        self._lock = threading.Lock()
        self._by: dict[tuple[str, str], _SessionLang] = {}

    def start(self, room_id: str, session_id: str, lang: str | None = None) -> str:
        lang = validate_tgt_lang(lang or self.default)
        with self._lock:
            self._by[(room_id, session_id)] = _SessionLang(lang, 0, [(0, lang, self._clock())])
        return lang

    def switch(self, room_id: str, session_id: str, lang: str, *, last_seq: int) -> dict | None:
        """Apply ``lang`` from ``last_seq + 1``. Returns the event to record, or None if unchanged."""
        lang = validate_tgt_lang(lang)
        with self._lock:
            cur = self._by.get((room_id, session_id))
            if cur is None:
                cur = self._by[(room_id, session_id)] = _SessionLang(self.default, 0, [(0, self.default, self._clock())])
            if cur.history[-1][1] == lang:
                return None
            frm = max(int(last_seq) + 1, cur.history[-1][0])
            cur.history.append((frm, lang, self._clock()))
            cur.lang, cur.from_seq = lang, frm
        return {"kind": "tgt_lang_changed", "room_id": room_id, "session_id": session_id,
                "tgt_lang": lang, "from_seq": frm}

    def lang_for(self, room_id: str, session_id: str, seq: int) -> str:
        with self._lock:
            cur = self._by.get((room_id, session_id))
            if cur is None:
                return self.default
            lang = cur.history[0][1]
            for frm, value, _ in cur.history:
                if seq >= frm:
                    lang = value
            return lang

    def end(self, room_id: str, session_id: str) -> None:
        with self._lock:
            self._by.pop((room_id, session_id), None)


# ---------------------------------------------------------------- caption gates
_MARKUP = re.compile(r"(?i)</?think\b|<\|[^|]{0,40}\|>|(?:^|\s)/(?:no_)?think\b")
_JA_SCRIPT = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff\u3400-\u4dbf\uff66-\uff9f]")
_CTRL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f\u2028\u2029]")
_KANA = re.compile(r"[\u3040-\u30ff\uff66-\uff9f]")
_NOT_TEXT = re.compile(r"[\s\u3000-\u303f\uff00-\uff0f\uff1a-\uff20\.,!?;:'\"()\[\]-]")
JA_KANA_FREE_MAX = 8


def _bare(text: str) -> str:
    return _NOT_TEXT.sub("", text or "")


def ja_copy_of_source(body: str, zh: str) -> bool:
    """round3 C5: the model echoed the Chinese instead of translating it."""
    import difflib
    b, z = _bare(body), _bare(zh)
    if not b or not z:
        return False
    if b == z:
        return True
    if _KANA.search(b):
        return False
    return difflib.SequenceMatcher(None, b, z).ratio() >= 0.8


def validate_caption(text: str, tgt_lang: str, *, zh: str = "", glossary=None) -> str | None:
    lang = validate_tgt_lang(tgt_lang)
    if lang == "en":
        return validate_caption_en(text, zh=zh, glossary=glossary)
    body = (text or "").strip()
    if not body or len(body) > max(200, 4 * len(zh or "")):
        return None
    if _MARKUP.search(body) or _CTRL.search(body) or "\n" in body:
        return None
    if not _JA_SCRIPT.search(body):
        return None                    # an English (or empty-script) reply is not a ja caption
    if ja_copy_of_source(body, zh):
        return None                    # C5: copied Chinese
    if not _KANA.search(body) and len(_bare(body)) > JA_KANA_FREE_MAX:
        return None                    # C5: real Japanese sentences carry kana (particles, endings)
    if body.startswith(("```", "{", "[")):
        return None
    return body


# ---------------------------------------------------------------- backend protocol + Hy-MT2
class MtBackend(Protocol):
    name: str

    def translate(self, zh: str, *, tgt_lang: str, glossary=None, context=None,
                  deadline: float | None = None, cancel: threading.Event | None = None) -> TranslateResult: ...


@dataclass
class HyMtLlamaServer:
    base_url: str = DEFAULT_BASE
    model: str = "hy-mt2-1.8b"
    prompt: str = DEFAULT_PROMPT
    temperature: float = 0.2
    max_tokens: int = 256
    opener: object | None = None
    name: str = "hymt"
    calls: int = 0

    def __post_init__(self):
        from app.translate_config import validate_base_url
        self.base_url = validate_base_url(self.base_url)          # loopback only, never 8645

    def build_messages(self, zh: str, tgt_lang: str, glossary=None, context=None) -> list[dict]:
        """Model-card templates (C6). context = earlier finalised zh lines (last CONTEXT_LINES used)
        -> "Structured Data 2"; otherwise the default template. Locked/known terms go in front as
        the card's terminology block. Glossary+context together is our composition (the card has
        no combined template) - UNVERIFIED, compare in the 20-sentence test."""
        lang = validate_tgt_lang(tgt_lang)
        name = LANG_NAMES[lang]
        background = [str(x).strip() for x in (context or []) if str(x or "").strip()][-CONTEXT_LINES:]
        if background:
            text = CONTEXT_PROMPT.format(background_text="\n".join(background), target_language=name, source_text=zh)
        else:
            text = self.prompt.format(target_language=name, source_text=zh)
        terms = [t for t in (glossary or []) if isinstance(t, dict) and t.get("zh")]
        if terms:
            key = "en" if lang == "en" else "ja"
            pairs = [TERM_LINE.format(src=t["zh"], tgt=t.get(key) or t.get("tgt"))
                     for t in terms if t.get(key) or t.get("tgt")]
            if pairs:
                text = TERMS_HEADER + "\n".join(pairs) + "\n" + text
        return [{"role": "user", "content": text}]

    def translate(self, zh: str, *, tgt_lang: str, glossary=None, context=None,
                  deadline: float | None = None, cancel: threading.Event | None = None) -> TranslateResult:
        if not zh:
            return TranslateResult("", "off")
        try:
            lang = validate_tgt_lang(tgt_lang)
        except TargetLangError as exc:
            return TranslateResult("", "error", str(exc))
        if cancel is not None and cancel.is_set():
            return TranslateResult("", "timeout", "已取消")
        timeout = 30.0
        if deadline is not None:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                return TranslateResult("", "timeout", "翻譯逾時，中文仍保留")
        body = {"model": self.model, "messages": self.build_messages(zh, lang, glossary, context),
                "temperature": self.temperature, "max_tokens": self.max_tokens, "stream": False}
        req = urllib.request.Request(self.base_url.rstrip("/") + "/chat/completions",
                                     data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        from app.net import safe_opener
        open_url = self.opener or safe_opener()
        self.calls += 1
        try:
            with open_url(req, timeout=min(30.0, max(0.05, timeout))) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            choice = data["choices"][0]
            content = (choice.get("message") or {}).get("content")
            if choice.get("finish_reason") == "length" or not isinstance(content, str):
                return TranslateResult("", "bad_response", "翻譯被截斷或無法讀取，中文仍保留")
        except urllib.error.HTTPError as exc:
            return TranslateResult("", "http", f"llama-server HTTP {exc.code}")
        except TimeoutError:
            return TranslateResult("", "timeout", "翻譯逾時，中文仍保留")
        except (urllib.error.URLError, OSError):
            return TranslateResult("", "network", "連不到本機翻譯伺服器，中文仍保留")
        except (KeyError, IndexError, TypeError, ValueError):
            return TranslateResult("", "bad_response", "翻譯回應無法讀取，中文仍保留")
        accepted = validate_caption(content, lang, zh=zh, glossary=glossary)
        if accepted is None:
            return TranslateResult("", "bad_response", "譯文不符合字幕格式，中文仍保留")
        return TranslateResult(accepted, "ok")


def backend_from_env(env=None, opener=None) -> MtBackend | None:
    env = os.environ if env is None else env
    mode = (env.get("BREEZE_MT_BACKEND") or "off").strip().lower()
    if mode in ("", "off", "0"):
        return None
    if mode != "hymt":
        raise ValueError("BREEZE_MT_BACKEND 只能是 off 或 hymt")

    def num(name, default, cast):
        try:
            return cast((env.get(name) or "").strip() or default)
        except ValueError:
            return default
    return HyMtLlamaServer(base_url=env.get("BREEZE_MT_BASE_URL") or DEFAULT_BASE,
                           model=env.get("BREEZE_MT_MODEL") or "hy-mt2-1.8b",
                           prompt=env.get("BREEZE_MT_PROMPT") or DEFAULT_PROMPT,
                           temperature=num("BREEZE_MT_TEMPERATURE", 0.2, float),
                           max_tokens=num("BREEZE_MT_MAX_TOKENS", 256, int), opener=opener)
