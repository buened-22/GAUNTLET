"""Request-origin checks for the local API.

The API has no authentication: it is meant to be used by its operator, on the loopback interface, from the
command line or the local dashboard. Binding to 127.0.0.1 does not by itself keep other parties out, because
the operator's own web browser can reach the loopback interface on behalf of any web page:

* **DNS rebinding.** A page on ``evil.example`` re-resolves its own name to 127.0.0.1 and then talks to this
  API as a same-origin site. The browser still sends ``Host: evil.example``, so requests whose ``Host`` is
  not a loopback name are refused.
* **Cross-site requests.** A page on another origin submits a request to ``http://127.0.0.1:8000`` directly.
  Browsers attach an ``Origin`` header to such requests, so state-changing requests with an ``Origin`` that
  is neither the local dashboard nor this server itself are refused, and so are request bodies that are not
  ``application/json`` (a cross-site page cannot send that media type without a CORS preflight, which is
  refused for foreign origins).

Without these checks a web page could start a candidate evaluation with ``trusted_environment: true``, that
is, make the operator's trust declaration on the operator's behalf.

These checks stop a browser from being used as a relay. They are not authentication: any local process, and
anyone who can reach the port directly, can still use the API. Never expose it on a network.
"""

from __future__ import annotations

import json
import os

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
DASHBOARD_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def _env_list(name: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


def allowed_hosts() -> list[str]:
    """Host names a request may be addressed to: loopback names, plus ``GAUNTLET_API_ALLOWED_HOSTS``."""
    return [*LOOPBACK_HOSTS, *(h.lower() for h in _env_list("GAUNTLET_API_ALLOWED_HOSTS"))]


def allowed_origins() -> list[str]:
    """Origins of the local dashboard, plus ``GAUNTLET_API_ALLOWED_ORIGINS`` (also used for CORS)."""
    return [*DASHBOARD_ORIGINS, *(o.rstrip("/") for o in _env_list("GAUNTLET_API_ALLOWED_ORIGINS"))]


def host_name(host_header: str) -> str:
    """The host part of a ``Host`` header (or of an origin's authority), without the port, lower-cased."""
    h = host_header.strip().lower()
    if h.startswith("["):  # IPv6 literal: [::1]:8000
        return h[1:].split("]", 1)[0]
    return h.rsplit(":", 1)[0] if ":" in h else h


def is_json(content_type: str) -> bool:
    media = content_type.split(";", 1)[0].strip().lower()
    return media == "application/json" or (media.startswith("application/") and media.endswith("+json"))


class LocalRequestGuard:
    """ASGI middleware applying the checks described in the module docstring. Refusals are 403 or 415 JSON
    responses produced before the request reaches any route, so nothing is queued or executed."""

    def __init__(self, app, hosts: list[str] | None = None, origins: list[str] | None = None):
        self.app = app
        self.hosts = set(hosts if hosts is not None else allowed_hosts())
        self.origins = set(origins if origins is not None else allowed_origins())

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers: dict[str, str] = {}
        for k, v in scope.get("headers") or []:
            headers.setdefault(k.decode("latin-1").lower(), v.decode("latin-1"))
        host = headers.get("host", "")
        if host_name(host) not in self.hosts:
            await self._refuse(scope, send, 403, "the Host header of this request is not a local address; the "
                               "GAUNTLET API only answers requests addressed to the loopback interface "
                               "(set GAUNTLET_API_ALLOWED_HOSTS to allow another name)")
            return
        if scope["type"] == "http" and scope.get("method", "GET").upper() not in SAFE_METHODS:
            origin = headers.get("origin")
            if origin is not None:
                o = origin.strip().rstrip("/").lower()
                same_server = "://" in o and o.split("://", 1)[1] == host.strip().lower()
                if o not in self.origins and not same_server:
                    await self._refuse(scope, send, 403, "state-changing requests from this web origin are not "
                                       "accepted (set GAUNTLET_API_ALLOWED_ORIGINS to allow another dashboard origin)")
                    return
            has_body = headers.get("content-length", "0").strip() not in ("", "0") or "transfer-encoding" in headers
            if has_body and not is_json(headers.get("content-type", "")):
                await self._refuse(scope, send, 415, "request bodies must be sent as application/json")
                return
        await self.app(scope, receive, send)

    @staticmethod
    async def _refuse(scope, send, status: int, detail: str) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"detail": detail}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
