"""Armazenamento de páginas em Markdown, espelhando a hierarquia de paths.

Cada página vira host/caminho/index.md com front matter (id, url, pai, profundidade...),
breadcrumb e lista de filhos com links relativos. Assim o repositório de conhecimento
pode ser navegado como árvore no GitHub, sem ferramenta extra.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from kom.db import emit, tx
from kom.factory import register
from kom.urls import canonical_url, page_relpath

MD_LINK = re.compile(r"\]\(([^)\s]+)((?:\s+\"[^\"]*\")?)\)")


def rewrite_internal_links(markdown: str, page_url: str, url_map: dict[str, str], here: str) -> str:
    """Links absolutos/relativos do site apontando para páginas coletadas viram caminhos relativos
    entre arquivos do repositório de conhecimento. Os demais links ficam como estão."""

    def sub(m: re.Match) -> str:
        target = canonical_url(m.group(1), page_url)
        if target and target in url_map:
            return f"]({posixpath.relpath(url_map[target], here)}{m.group(2)})"
        return m.group(0)

    return MD_LINK.sub(sub, markdown)


def render_page(page: dict, rel: str, parent: dict | None, children: list[dict], ancestors: list[dict],
                url_map: dict[str, str] | None = None) -> str:
    here = posixpath.dirname(rel) or "."

    def link(p: dict, text: str | None = None) -> str:
        target = page_relpath(p["url"])
        return f"[{text or p['title'] or p['url']}]({posixpath.relpath(target, here)})"

    fm = {
        "id": page["id"],
        "url": page["url"],
        "title": page["title"],
        "parent": parent["url"] if parent else "",
        "depth": page["depth"],
        "topo_index": page["topo_index"],
        "content_hash": page["content_hash"],
        "fetched_at": page["fetched_at"],
    }
    lines = ["---"] + [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in fm.items()] + ["---", ""]
    body = page["markdown"].rstrip()
    first = next((ln for ln in body.splitlines() if ln.strip()), "")
    if not first.startswith("# "):  # evita H1 duplicado quando o próprio conteúdo já traz o título
        lines.append(f"# {page['title'] or page['url']}")
        lines.append("")
    if ancestors:
        crumbs = [link(a) for a in ancestors] + [f"**{page['title'] or page['url']}**"]
        lines.append("Caminho: " + " › ".join(crumbs))
        lines.append("")
    if url_map:
        body = rewrite_internal_links(body, page["url"], url_map, here)
    lines.append(body)
    if children:
        lines += ["", "## Filhos", ""] + [f"- {link(c)}" for c in children]
    return "\n".join(lines) + "\n"


class MarkdownSink:
    name = "filesystem"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def write_page(self, page: dict, parent: dict | None, children: list[dict], ancestors: list[dict],
                   url_map: dict[str, str] | None = None) -> str:
        rel = page_relpath(page["url"])
        body = render_page(page, rel, parent, children, ancestors, url_map)
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_text(encoding="utf-8") == body:
            return rel  # idempotente: nada mudou
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, target)  # escrita atômica
        return rel


def store_crawl(conn: sqlite3.Connection, crawl_id: str, sink: Any) -> int:
    rows = [dict(r) for r in conn.execute(
        """SELECT id, url, depth, title, markdown, content_hash, fetched_at, parent_id, topo_index
           FROM pages WHERE crawl_id = ?""",
        (crawl_id,),
    )]
    by_id = {r["id"]: r for r in rows}
    children: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["parent_id"]:
            children[r["parent_id"]].append(r)
    for kids in children.values():
        kids.sort(key=lambda r: r["url"])

    url_map = {r["url"]: page_relpath(r["url"]) for r in rows}
    updates = []
    for r in rows:
        ancestors: list[dict] = []
        cur = by_id.get(r["parent_id"]) if r["parent_id"] else None
        while cur is not None:
            ancestors.append(cur)
            cur = by_id.get(cur["parent_id"]) if cur["parent_id"] else None
        ancestors.reverse()  # raiz primeiro
        parent = ancestors[-1] if ancestors else None
        rel = sink.write_page(r, parent, children.get(r["id"], []), ancestors, url_map)
        updates.append((rel, r["id"]))

    with tx(conn):
        conn.executemany("UPDATE pages SET file_path = ? WHERE id = ?", updates)
        emit(conn, crawl_id, "store.written", {"files": len(updates)})
    return len(updates)


@register("sink", "filesystem")
def _make_sink(settings) -> MarkdownSink:
    return MarkdownSink(settings.out_dir)
