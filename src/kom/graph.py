"""Grafo de conhecimento de um crawl.

Arestas:
- parent     : herança por path (pai -> filho), estrutura org-mode
- links_to   : hyperlink do conteúdo principal entre páginas coletadas
- similar    : similaridade semântica (enricher)

Organização topológica: ordem em que pai aparece antes de filho (graphlib).
Idempotente: recalcula e substitui as arestas do crawl dentro de uma transação.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from graphlib import TopologicalSorter
from typing import Any

from kom.db import emit, tx
from kom.urls import parent_candidates


def resolve_parents(pages: list[dict]) -> tuple[dict[str, str], dict[str, str | None]]:
    by_url = {p["url"]: p["id"] for p in pages}
    parent: dict[str, str | None] = {}
    for p in pages:
        parent[p["id"]] = None
        for cand in parent_candidates(p["url"]):
            pid = by_url.get(cand)
            if pid and pid != p["id"]:
                parent[p["id"]] = pid
                break
    return by_url, parent


def topo_index(ids: list[str], parent: dict[str, str | None]) -> dict[str, int]:
    ts: TopologicalSorter[str] = TopologicalSorter()
    for pid in ids:
        par = parent.get(pid)
        ts.add(pid, *([par] if par else []))
    return {pid: i for i, pid in enumerate(ts.static_order())}


def build_tree(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Árvore aninhada a partir de parent_id (para a API e exportação)."""
    nodes = {p["id"]: {"id": p["id"], "url": p["url"], "title": p["title"],
                       "depth": p["depth"], "children": []} for p in pages}
    roots = []
    for p in pages:
        node = nodes[p["id"]]
        if p.get("parent_id") and p["parent_id"] in nodes:
            nodes[p["parent_id"]]["children"].append(node)
        else:
            roots.append(node)
    return roots


def build_graph(conn: sqlite3.Connection, crawl_id: str, enricher: Any) -> dict[str, Any]:
    pages = [dict(r) for r in conn.execute(
        "SELECT id, url, depth, title, markdown FROM pages WHERE crawl_id = ? ORDER BY depth, url",
        (crawl_id,),
    )]
    by_url, parent = resolve_parents(pages)
    topo = topo_index([p["id"] for p in pages], parent)

    edges: dict[tuple[str, str, str], float] = {}

    def add(src: str, dst: str, kind: str, weight: float) -> None:
        if src == dst:
            return
        key = (src, dst, kind)
        edges[key] = max(weight, edges.get(key, 0.0))

    for pid, par in parent.items():
        if par:
            add(par, pid, "parent", 1.0)

    link_rows = conn.execute(
        "SELECT l.src_id, l.dst_url FROM links l JOIN pages p ON p.id = l.src_id WHERE p.crawl_id = ?",
        (crawl_id,),
    ).fetchall()
    link_updates = []
    for r in link_rows:
        dst = by_url.get(r["dst_url"])
        link_updates.append((dst, r["src_id"], r["dst_url"]))
        if dst:
            add(r["src_id"], dst, "links_to", 1.0)

    for src, dst, w in enricher.similar_edges(pages):
        add(src, dst, "similar", w)

    with tx(conn):
        conn.executemany("UPDATE links SET dst_id = ? WHERE src_id = ? AND dst_url = ?", link_updates)
        conn.executemany(
            "UPDATE pages SET parent_id = ?, topo_index = ? WHERE id = ?",
            [(parent[p["id"]], topo[p["id"]], p["id"]) for p in pages],
        )
        conn.execute("DELETE FROM graph_edges WHERE crawl_id = ?", (crawl_id,))
        conn.executemany(
            "INSERT INTO graph_edges (crawl_id, src_id, dst_id, kind, weight) VALUES (?, ?, ?, ?, ?)",
            [(crawl_id, s, d, k, w) for (s, d, k), w in edges.items()],
        )
        counts = dict(Counter(k for (_, _, k) in edges))
        emit(conn, crawl_id, "graph.built", {"pages": len(pages), "edges": counts})
    return {"pages": len(pages), "edges": counts}
