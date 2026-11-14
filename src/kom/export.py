"""Exporta a API estática (JSON) para ser servida por GitHub Pages.

site/api/index.json                      lista de crawls
site/api/crawls/<crawl_id>.json          metadados, páginas, arestas, árvore, ordem topológica
site/api/crawls/<crawl_id>/pages/<id>.json  página completa com markdown
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from kom.graph import build_tree


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def export_crawl(conn: sqlite3.Connection, crawl_id: str, site_dir: str) -> Path:
    root = Path(site_dir)
    crawl = conn.execute("SELECT * FROM crawls WHERE id = ?", (crawl_id,)).fetchone()
    pages = [dict(r) for r in conn.execute(
        """SELECT id, url, title, depth, parent_id, topo_index, file_path, content_hash,
                  fetched_at, markdown
           FROM pages WHERE crawl_id = ? ORDER BY topo_index, url""",
        (crawl_id,),
    )]
    edges = [dict(r) for r in conn.execute(
        "SELECT src_id AS source, dst_id AS target, kind, weight FROM graph_edges WHERE crawl_id = ?",
        (crawl_id,),
    )]
    meta = [{k: v for k, v in p.items() if k != "markdown"} for p in pages]

    _write_json(root / "crawls" / f"{crawl_id}.json", {
        "crawl": dict(crawl),
        "pages": meta,
        "edges": edges,
        "topo": [p["id"] for p in pages],
        "tree": build_tree(pages),
    })
    for p in pages:
        _write_json(root / "crawls" / crawl_id / "pages" / f"{p['id']}.json", p)

    crawls = [dict(r) for r in conn.execute(
        "SELECT id, root_url, host, status, stage, created_at, updated_at FROM crawls ORDER BY created_at"
    )]
    _write_json(root / "index.json", {"crawls": crawls})
    return root
