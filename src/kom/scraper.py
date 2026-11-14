"""Fetcher HTTP educado: respeita robots.txt, intervalo entre requisições e timeouts."""

from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.robotparser import RobotFileParser
from urllib.parse import urlsplit

import httpx

from kom.factory import register
from kom.urls import canonical_url


class PermanentFetchError(Exception):
    """Erro que não adianta tentar de novo (ex.: 404/410)."""


@dataclass(frozen=True)
class FetchResult:
    url: str  # URL final após redirecionamentos
    status: int
    content_type: str
    text: str  # vazio se não for HTML


class HttpFetcher:
    name = "http"

    def __init__(self, settings, transport: httpx.BaseTransport | None = None) -> None:
        self.user_agent = settings.user_agent
        self.delay_s = settings.delay_s
        self._client = httpx.Client(
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
            },
            timeout=settings.timeout_s,
            follow_redirects=True,
            transport=transport,
        )
        self._robots: dict[str, RobotFileParser] = {}
        self._last = 0.0

    def _polite_wait(self) -> None:
        wait = self.delay_s - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        rp = self._robots.get(base)
        if rp is None:
            rp = RobotFileParser()
            lines: list[str] = []
            try:
                r = self._client.get(base + "/robots.txt", timeout=10)
                if r.status_code == 200:
                    lines = r.text.splitlines()
            except httpx.HTTPError:
                lines = []  # sem robots.txt acessível: assume permitido
            rp.parse(lines)
            self._robots[base] = rp
        return rp.can_fetch(self.user_agent, url)

    def fetch(self, url: str) -> FetchResult:
        self._polite_wait()
        r = self._client.get(url)
        if r.status_code in (404, 410):
            raise PermanentFetchError(f"HTTP {r.status_code}")
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}")  # transitório: será retentado
        ct = r.headers.get("content-type", "")
        text = r.text if "html" in ct.lower() else ""
        return FetchResult(canonical_url(str(r.url)) or url, r.status_code, ct, text)

    def close(self) -> None:
        self._client.close()


@register("fetcher", "http")
def _make_http(settings) -> HttpFetcher:
    return HttpFetcher(settings)
