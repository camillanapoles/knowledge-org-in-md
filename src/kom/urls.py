"""Normalização de URLs, escopo hierárquico (herança por path) e caminhos de armazenamento.

Herança: a página /docs/guide/setup tem como pai /docs/guide, que tem como pai /docs,
que tem como pai /. O pai é sempre o ancestral mais próximo que foi de fato coletado.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from pathlib import PurePosixPath
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

SKIP_EXTENSIONS = (
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
    ".zip", ".gz", ".tar", ".mp3", ".mp4", ".css", ".js", ".xml", ".json",
    ".woff", ".woff2", ".ttf",
)


def _segments(path: str) -> list[str]:
    """Segmentos do path com resolução de '.' e '..'. Barras duplicadas são ignoradas."""
    out: list[str] = []
    for seg in path.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if out:
                out.pop()
            continue
        out.append(seg)
    return out


def canonical_url(url: str, base: str | None = None) -> str | None:
    """Forma canônica: esquema/host minúsculos, sem porta padrão, sem query/fragmento,
    sem barra final. Retorna None se não for http(s) válido."""
    try:
        raw = urljoin(base, url.strip()) if base else url.strip()
        parts = urlsplit(raw)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        port = parts.port
    except ValueError:
        return None
    default_port = 80 if parts.scheme == "http" else 443
    host = parts.hostname.lower()
    netloc = host if port in (None, default_port) else f"{host}:{port}"
    path = "/" + "/".join(_segments(parts.path))
    return urlunsplit((parts.scheme, netloc, path, "", ""))


def scope_segments(url: str) -> list[str]:
    """Segmentos do path raiz. Um crawl só coleta URLs cujo path começa por estes segmentos."""
    return _segments(urlsplit(url).path)


def in_scope(url: str, netloc: str, scope: list[str]) -> bool:
    parts = urlsplit(url)
    return parts.netloc == netloc and _segments(parts.path)[: len(scope)] == scope


def should_skip(url: str) -> bool:
    segs = _segments(urlsplit(url).path)
    last = (segs[-1] if segs else "").lower()
    return last.endswith(SKIP_EXTENSIONS)


def parent_candidates(url: str) -> Iterator[str]:
    """Ancestores por path, do mais próximo ao raiz: /a/b/c -> /a/b, /a, /."""
    parts = urlsplit(url)
    segs = _segments(parts.path)
    for k in range(len(segs) - 1, -1, -1):
        path = "/" + "/".join(segs[:k])
        yield urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def page_id(crawl_id: str, url: str) -> str:
    """ID estável: mesmo crawl + mesma URL sempre produz o mesmo ID (idempotência)."""
    return "pg_" + hashlib.sha256(f"{crawl_id}|{url}".encode()).hexdigest()[:16]


def _safe(part: str) -> str:
    return re.sub(r"[^\w.\-]+", "-", part).strip("-.") or "_"


def page_relpath(url: str) -> str:
    """Caminho no repositório de conhecimento, espelhando o path da URL:
    https://ex.com/docs/guide/setup -> ex.com/docs/guide/setup/index.md
    Cada página é uma pasta, então os filhos ficam aninhados dentro dela."""
    parts = urlsplit(url)
    segs = [_safe(unquote(s)) for s in _segments(parts.path)]
    return PurePosixPath(_safe(parts.netloc.replace(":", "_")), *segs, "index.md").as_posix()
