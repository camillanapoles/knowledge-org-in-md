"""Banco relacional (SQLite). Todos os registros têm IDs únicos e chaves estrangeiras."""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import os
import sqlite3
from collections.abc import Iterator

log = logging.getLogger("kom")

SCHEMA = """
CREATE TABLE IF NOT EXISTS crawls (
  id            TEXT PRIMARY KEY,
  root_url      TEXT NOT NULL,
  host          TEXT NOT NULL,
  scope         TEXT NOT NULL DEFAULT '',
  max_depth     INTEGER NOT NULL,
  max_pages     INTEGER NOT NULL,
  status        TEXT NOT NULL CHECK (status IN ('queued','running','done','failed')),
  stage         TEXT NOT NULL DEFAULT 'scrape'
                CHECK (stage IN ('scrape','graph','store','export','done')),
  stage_attempts INTEGER NOT NULL DEFAULT 0,
  error         TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  crawl_id        TEXT NOT NULL REFERENCES crawls(id) ON DELETE CASCADE,
  url             TEXT NOT NULL,
  depth           INTEGER NOT NULL,
  discovered_from TEXT,
  state           TEXT NOT NULL CHECK (state IN ('queued','running','done','skipped','dead')),
  attempts        INTEGER NOT NULL DEFAULT 0,
  lease_until     REAL NOT NULL DEFAULT 0,
  error           TEXT,
  updated_at      TEXT NOT NULL,
  UNIQUE (crawl_id, url)
);
CREATE INDEX IF NOT EXISTS ix_tasks_claim ON tasks (state, lease_until, depth, id);

CREATE TABLE IF NOT EXISTS pages (
  id           TEXT PRIMARY KEY,
  crawl_id     TEXT NOT NULL REFERENCES crawls(id) ON DELETE CASCADE,
  url          TEXT NOT NULL,
  host         TEXT NOT NULL,
  depth        INTEGER NOT NULL,
  title        TEXT NOT NULL DEFAULT '',
  markdown     TEXT NOT NULL DEFAULT '',
  content_hash TEXT NOT NULL DEFAULT '',
  fetched_at   TEXT NOT NULL,
  parent_id    TEXT REFERENCES pages(id) ON DELETE SET NULL,
  topo_index   INTEGER,
  file_path    TEXT,
  UNIQUE (crawl_id, url)
);
CREATE INDEX IF NOT EXISTS ix_pages_parent ON pages (parent_id);

CREATE TABLE IF NOT EXISTS links (
  src_id  TEXT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
  dst_url TEXT NOT NULL,
  dst_id  TEXT,
  PRIMARY KEY (src_id, dst_url)
);

CREATE TABLE IF NOT EXISTS graph_edges (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  crawl_id TEXT NOT NULL REFERENCES crawls(id) ON DELETE CASCADE,
  src_id   TEXT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
  dst_id   TEXT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
  kind     TEXT NOT NULL CHECK (kind IN ('parent','links_to','similar')),
  weight   REAL NOT NULL DEFAULT 1.0,
  UNIQUE (src_id, dst_id, kind)
);

-- Log de eventos: trilha de auditoria do gestor de eventos.
CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  crawl_id   TEXT REFERENCES crawls(id) ON DELETE CASCADE,
  type       TEXT NOT NULL,
  payload    TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # isolation_level=None: controle explícito de transações via tx().
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


@contextlib.contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Transação atômica. BEGIN IMMEDIATE evita conflito entre workers."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def emit(conn: sqlite3.Connection, crawl_id: str | None, etype: str, payload: dict | None = None) -> None:
    """Gestor de eventos: persiste o evento (durável) e registra em log."""
    data = payload or {}
    conn.execute(
        "INSERT INTO events (crawl_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
        (crawl_id, etype, json.dumps(data, ensure_ascii=False), now_iso()),
    )
    log.info("%s crawl=%s %s", etype, crawl_id, data)
