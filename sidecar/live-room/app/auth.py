from __future__ import annotations

import hmac
import ipaddress
import secrets
from urllib.parse import urlparse

from fastapi import HTTPException, Request

from app.settings import Settings

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _canonical_host(name: str) -> str:
    """Exact host identity: case-insensitive, trailing dot stripped, IPv6 compressed."""
    text = (name or "").strip().lower().rstrip(".")
    if not text:
        return ""
    try:
        return ipaddress.ip_address(text).compressed
    except ValueError:
        return text


def _allowed_names(settings: Settings) -> set[str]:
    return {_canonical_host(name) for name in (*LOOPBACK, *settings.allowed_hosts)}


def new_host_token() -> str:
    return secrets.token_urlsafe(32)


def client_host(request: Request) -> str:
    if request.client is None:
        return ""
    return request.client.host or ""


def _split_host(value: str) -> tuple[str, int | None]:
    text = (value or "").strip()
    if not text:
        return "", None
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            return "", None
        name = text[1:end]
        rest = text[end + 1 :]
        if rest == "":
            return name, None
        if not rest.startswith(":"):
            return "", None
        try:
            return name, int(rest[1:])
        except ValueError:
            return "", None
    if text.count(":") == 1:
        name, port_s = text.rsplit(":", 1)
        try:
            return name, int(port_s)
        except ValueError:
            return "", None
    return text, None


def _name_set(settings: Settings, extra_hosts: tuple[str, ...] = ()) -> set[str]:
    names = set(_allowed_names(settings))
    for item in extra_hosts:
        canon = _canonical_host(item)
        if canon:
            names.add(canon)
    return names


def same_secret(supplied: str, expected: str) -> bool:
    """Constant-time compare. Different lengths and non-text values are simply not equal."""
    if not isinstance(supplied, str) or not isinstance(expected, str):
        return False
    if not supplied or not expected:
        return False
    try:
        left = supplied.encode("utf-8")
        right = expected.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(left, right)


def host_is_allowed(host_header: str, settings: Settings, extra_hosts: tuple[str, ...] = ()) -> bool:
    name, port = _split_host(host_header)
    if _canonical_host(name) not in _name_set(settings, extra_hosts):
        return False
    # This service does not sit on port 80. A missing port is not an exact match.
    return port == settings.port


def origin_is_allowed(origin: str, settings: Settings, extra_hosts: tuple[str, ...] = ()) -> bool:
    parsed = urlparse(origin.strip())
    if parsed.scheme not in set(settings.allowed_schemes):
        return False
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    if parsed.path not in {"", "/"}:
        return False
    host = _canonical_host(parsed.hostname or "")
    if host not in _name_set(settings, extra_hosts):
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    if port is None:
        return host in {_canonical_host(name) for name in LOOPBACK}
    return port == settings.port


def audience_origin_allowed(
    origin: str | None,
    host_header: str,
    settings: Settings,
    extra_hosts: tuple[str, ...] = (),
) -> bool:
    """Audience sockets may use the share host printed on the QR code.

    Browsers always send Origin. A missing Origin is allowed so the existing
    non-browser clients (the test sockets and scripts/device_check.py) can connect.
    A present Origin still has to match the allowlist, which blocks a cross-site page.
    """
    if not host_is_allowed(host_header, settings, extra_hosts):
        return False
    if origin is None or not str(origin).strip():
        return True
    return origin_is_allowed(str(origin), settings, extra_hosts)


def require_local_host(request: Request, settings: Settings) -> None:
    """Issue a host token only to a real loopback browser, or the explicit test client."""
    peer = client_host(request)
    if settings.allow_testclient and peer == "testclient":
        return
    if peer not in {"127.0.0.1", "::1"}:
        raise HTTPException(status_code=403, detail="請在主持這台電腦的瀏覽器開啟，不要從別的裝置拿主持權杖")
    # X-Forwarded-For / X-Forwarded-Host are ignored on purpose.
    if not host_is_allowed(request.headers.get("host", ""), settings):
        raise HTTPException(status_code=403, detail="主持頁的位址不在允許清單")
    origin = request.headers.get("origin")
    if origin and not origin_is_allowed(origin, settings):
        raise HTTPException(status_code=403, detail="這個頁面不能拿主持權杖")


def require_host(request: Request, token: str, settings: Settings) -> None:
    header = request.headers.get("authorization", "")
    supplied = ""
    if header.lower().startswith("bearer "):
        supplied = header[7:].strip()
    if not supplied or not hmac.compare_digest(supplied, token):
        raise HTTPException(status_code=401, detail="沒有主持權限")
    origin = request.headers.get("origin")
    if origin and not origin_is_allowed(origin, settings):
        raise HTTPException(status_code=403, detail="這個頁面不能操作主持端")
