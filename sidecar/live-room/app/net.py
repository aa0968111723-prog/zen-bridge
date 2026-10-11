"""Outbound HTTP helpers shared by the live room and the admin backend (資安長 #1).

* RESERVED_PORTS: 8645 is the Hermes gateway. Never bind, proxy or call it.
* safe_opener(): never follows redirects (a 302/307 from a fake "Ollama" could point at 8645
  and would carry the Authorization header) and, for loopback targets, ignores *_PROXY env.
"""
from __future__ import annotations

import urllib.request

RESERVED_PORTS = frozenset({8645})


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None            # urllib then raises HTTPError(3xx) instead of following


def safe_opener(*, use_proxy: bool = False):
    handlers = [NoRedirect()]
    if not use_proxy:
        handlers.insert(0, urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers).open


def effective_port(parts) -> int:
    port = parts.port
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return port
