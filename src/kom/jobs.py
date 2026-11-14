"""Fila de trabalho durável em SQLite.

- Uma tarefa é "claimed" com lease. Se o worker morrer, o lease expira e
  `reclaim_expired` a devolve para a fila (retomada após queda).
- Tentativas com backoff exponencial; após max_attempts vira 'dead'.
- Estágios do crawl (graph -> store -> export) também são idempotentes e
  retentados (ver pipeline.py).
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from typing import Any
from urllib.parse import urlsplit

from kom.db import emit, now_iso, tx
from kom.urls import canonical_url, scope_segments


def create_crawl(conn: sqlite3.Connection, root_url: str, max_depth: int = 2, max_pages: int = 200) -> str:
    url = canonical_url(root_url)
    if url is None:
        raise ValueError(f"URL inválida (use http/https): {root_url!r}")
    crawl_id = "cr_" + uuid.uuid4().hex[:12]
    now = now_iso()
    parts = urlsplit(url)
    scope = "/" + "/".join(scope_segments(url))
    with tx(conn):
        conn.execute(
            """INSERT INTO crawls (id, root_url, host, scope, max_depth, max_pages,
                                   status, stage, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'queued', 'scrape', ?, ?)""",
            (crawl_id, url, parts.netloc, scope, max_depth, max_pages, now, now),
        )
        conn.execute(
            """INSERT INTO tasks (crawl_id, url, depth, discovered_from, state, updated_at)
               VALUES (?, ?, 0, NULL, 'queued', ?)""",
            (crawl_id, url, now),
        )
        emit(conn, crawl_id, "crawl.created", {"root_url": url, "max_depth": max_depth, "max_pages": max_pages})
    return crawl_id


def get_crawl(conn: sqlite3.Connection, crawl_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM crawls WHERE id = ?", (crawl_id,)).fetchone()


def claim_task(conn: sqlite3.Connection, lease_s: float) -> sqlite3.Row | None:
    now = time.time()
    with tx(conn):
        row = conn.execute(
            """SELECT t.id FROM tasks t JOIN crawls c ON c.id = t.crawl_id
               WHERE t.state = 'queued' AND t.lease_until <= ?
                 AND c.status IN ('queued', 'running')
               ORDER BY t.depth, t.id LIMIT 1""",
            (now,),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE tasks SET state = 'running', attempts = attempts + 1, lease_until = ?, updated_at = ? WHERE id = ?",
            (now + lease_s, now_iso(), row["id"]),
        )
        conn.execute(
            """UPDATE crawls SET status = 'running', updated_at = ?
               WHERE status = 'queued' AND id = (SELECT crawl_id FROM tasks WHERE id = ?)""",
            (now_iso(), row["id"]),
        )
        return conn.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()


def reclaim_expired(conn: sqlite3.Connection) -> int:
    """Recupera tarefas cujo worker caiu (lease vencido)."""
    now = time.time()
    with tx(conn):
        cur = conn.execute(
            "UPDATE tasks SET state = 'queued', updated_at = ? WHERE state = 'running' AND lease_until <= ?",
            (now_iso(), now),
        )
        n = cur.rowcount
        if n:
            emit(conn, None, "task.reclaimed", {"count": n})
    return n


def ready_crawl(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Crawl sem tarefas de scrape pendentes e com estágio por executar."""
    return conn.execute(
        """SELECT c.* FROM crawls c
           WHERE c.status IN ('queued', 'running') AND c.stage != 'done'
             AND NOT EXISTS (SELECT 1 FROM tasks t
                             WHERE t.crawl_id = c.id AND t.state IN ('queued', 'running'))
           ORDER BY c.created_at LIMIT 1"""
    ).fetchone()


def has_pending_work(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            """SELECT 1 FROM tasks t JOIN crawls c ON c.id = t.crawl_id
               WHERE t.state IN ('queued', 'running') AND c.status IN ('queued', 'running') LIMIT 1"""
        ).fetchone()
        is not None
    )


def resume_crawl(conn: sqlite3.Connection, crawl_id: str) -> None:
    with tx(conn):
        conn.execute(
            "UPDATE crawls SET status = 'queued', stage_attempts = 0, error = NULL, updated_at = ? WHERE id = ?",
            (now_iso(), crawl_id),
        )
        emit(conn, crawl_id, "crawl.resumed", {})


def crawl_summary(conn: sqlite3.Connection, crawl_id: str) -> dict[str, Any] | None:
    row = get_crawl(conn, crawl_id)
    if row is None:
        return None
    tasks = {
        r["state"]: r["n"]
        for r in conn.execute("SELECT state, COUNT(*) AS n FROM tasks WHERE crawl_id = ? GROUP BY state", (crawl_id,))
    }
    pages = conn.execute("SELECT COUNT(*) FROM pages WHERE crawl_id = ?", (crawl_id,)).fetchone()[0]
    out = dict(row)
    out["tasks"] = tasks
    out["pages"] = pages
    return out
