"""Pipeline e gestor de eventos.

Um crawl passa por estágios:  scrape -> graph -> store -> export -> done

- scrape: consome a fila de tarefas (uma página por tarefa). Cada página é gravada
  em uma única transação junto com a conclusão da tarefa e a descoberta dos filhos.
  Se o processo cair antes do COMMIT, nada fica pela metade; o lease expira e a tarefa é refeita.
- graph/store/export: cada um é idempotente. Se falhar, `stage_attempts` é incrementado
  e o estágio é reexecutado na próxima passada. Após `stage_max_attempts` o crawl vira 'failed'
  e pode ser retomado com `kom resume <id>`.
"""

from __future__ import annotations

import hashlib
import logging
import time

from kom import jobs
from kom.convert import Converted
from kom.db import emit, now_iso, tx
from kom.export import export_crawl
from kom.graph import build_graph
from kom.runtime import Runtime
from kom.scraper import PermanentFetchError
from kom.store import store_crawl
from kom.urls import in_scope, page_id, scope_segments, should_skip

log = logging.getLogger("kom")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _close_task(rt: Runtime, task, state: str, reason: str) -> None:
    with tx(rt.conn):
        rt.conn.execute(
            "UPDATE tasks SET state = ?, error = ?, lease_until = 0, updated_at = ? WHERE id = ?",
            (state, reason, now_iso(), task["id"]),
        )
        emit(rt.conn, task["crawl_id"], f"task.{state}", {"url": task["url"], "reason": reason})


def _retry(rt: Runtime, task, error: str) -> None:
    attempts = task["attempts"]
    with tx(rt.conn):
        if attempts >= rt.settings.max_attempts:
            rt.conn.execute(
                "UPDATE tasks SET state = 'dead', error = ?, lease_until = 0, updated_at = ? WHERE id = ?",
                (error, now_iso(), task["id"]),
            )
            emit(rt.conn, task["crawl_id"], "task.dead", {"url": task["url"], "error": error})
        else:
            delay = rt.settings.backoff_s * (2 ** (attempts - 1))
            rt.conn.execute(
                "UPDATE tasks SET state = 'queued', error = ?, lease_until = ?, updated_at = ? WHERE id = ?",
                (error, time.time() + delay, now_iso(), task["id"]),
            )
            emit(rt.conn, task["crawl_id"], "task.retry", {"url": task["url"], "attempt": attempts, "error": error})


def _save_page(rt: Runtime, crawl, task, final_url: str, conv: Converted) -> None:
    conn = rt.conn
    crawl_id = crawl["id"]
    url = task["url"]
    pid = page_id(crawl_id, url)
    netloc = crawl["host"]
    scope = scope_segments(crawl["root_url"])
    now = now_iso()
    discovered = 0
    with tx(conn):
        conn.execute(
            """INSERT INTO pages (id, crawl_id, url, host, depth, title, markdown, content_hash, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET title = excluded.title, markdown = excluded.markdown,
                   content_hash = excluded.content_hash, fetched_at = excluded.fetched_at""",
            (pid, crawl_id, url, netloc, task["depth"], conv.title, conv.markdown,
             _sha256(conv.markdown), now),
        )
        conn.execute("DELETE FROM links WHERE src_id = ?", (pid,))
        for dst in conv.links_content:
            conn.execute("INSERT OR IGNORE INTO links (src_id, dst_url) VALUES (?, ?)", (pid, dst))

        if task["depth"] < crawl["max_depth"]:
            count = conn.execute("SELECT COUNT(*) FROM tasks WHERE crawl_id = ?", (crawl_id,)).fetchone()[0]
            for cand in conv.links_all:
                if count >= crawl["max_pages"]:
                    break
                if not in_scope(cand, netloc, scope) or should_skip(cand):
                    continue
                cur = conn.execute(
                    """INSERT OR IGNORE INTO tasks (crawl_id, url, depth, discovered_from, state, updated_at)
                       VALUES (?, ?, ?, ?, 'queued', ?)""",
                    (crawl_id, cand, task["depth"] + 1, url, now),
                )
                if cur.rowcount:
                    count += 1
                    discovered += 1

        conn.execute(
            "UPDATE tasks SET state = 'done', error = NULL, lease_until = 0, updated_at = ? WHERE id = ?",
            (now, task["id"]),
        )
        emit(conn, crawl_id, "page.fetched", {"url": url, "title": conv.title, "discovered": discovered})


def process_task(rt: Runtime, task) -> None:
    crawl = rt.conn.execute("SELECT * FROM crawls WHERE id = ?", (task["crawl_id"],)).fetchone()
    url = task["url"]
    try:
        if not rt.fetcher.allowed(url):
            _close_task(rt, task, "skipped", "bloqueado por robots.txt")
            return
        res = rt.fetcher.fetch(url)
        if not res.text:
            _close_task(rt, task, "skipped", f"não é HTML ({res.content_type or 'sem content-type'})")
            return
        if not in_scope(res.url, crawl["host"], scope_segments(crawl["root_url"])):
            _close_task(rt, task, "skipped", f"redirecionado para fora do escopo: {res.url}")
            return
        conv = rt.converter.convert(res.text, res.url)
        _save_page(rt, crawl, task, res.url, conv)
    except PermanentFetchError as exc:
        _close_task(rt, task, "dead", str(exc))
    except Exception as exc:  # transitório: rede, 5xx, falha de parse...
        log.warning("falha em %s: %s", url, exc)
        _retry(rt, task, str(exc))


def advance(rt: Runtime, crawl) -> None:
    conn = rt.conn
    cid = crawl["id"]
    stage = crawl["stage"]
    try:
        if stage == "scrape":
            nxt = "graph"  # sem tarefas pendentes: a coleta terminou
        elif stage == "graph":
            build_graph(conn, cid, rt.enricher)
            nxt = "store"
        elif stage == "store":
            store_crawl(conn, cid, rt.sink)
            nxt = "export"
        elif stage == "export":
            export_crawl(conn, cid, rt.settings.site_dir)
            nxt = "done"
        else:
            raise RuntimeError(f"estágio desconhecido: {stage}")
        with tx(conn):
            status = "done" if nxt == "done" else "running"
            conn.execute(
                "UPDATE crawls SET stage = ?, status = ?, stage_attempts = 0, error = NULL, updated_at = ? WHERE id = ?",
                (nxt, status, now_iso(), cid),
            )
            emit(conn, cid, "stage.completed", {"stage": stage, "next": nxt})
    except Exception as exc:
        with tx(conn):
            conn.execute(
                "UPDATE crawls SET stage_attempts = stage_attempts + 1, error = ?, updated_at = ? WHERE id = ?",
                (str(exc), now_iso(), cid),
            )
            attempts = conn.execute("SELECT stage_attempts FROM crawls WHERE id = ?", (cid,)).fetchone()[0]
            if attempts >= rt.settings.stage_max_attempts:
                conn.execute("UPDATE crawls SET status = 'failed', updated_at = ? WHERE id = ?", (now_iso(), cid))
            emit(conn, cid, "stage.failed", {"stage": stage, "error": str(exc), "attempts": attempts})
        log.error("estágio %s falhou em %s: %s", stage, cid, exc)


def step(rt: Runtime) -> bool:
    """Executa uma unidade de trabalho. Retorna False se não há nada executável agora."""
    jobs.reclaim_expired(rt.conn)
    task = jobs.claim_task(rt.conn, rt.settings.lease_s)
    if task is not None:
        process_task(rt, task)
        return True
    crawl = jobs.ready_crawl(rt.conn)
    if crawl is not None:
        advance(rt, crawl)
        return True
    return False


def run_steps(rt: Runtime, max_steps: int = 1000) -> int:
    n = 0
    while n < max_steps and step(rt):
        n += 1
    return n


def drain(rt: Runtime, max_seconds: float = 3600.0) -> int:
    """Processa até não restar trabalho. Espera tarefas em backoff sem travar a fila."""
    deadline = time.monotonic() + max_seconds
    n = 0
    while time.monotonic() < deadline:
        if step(rt):
            n += 1
            continue
        if jobs.has_pending_work(rt.conn):
            time.sleep(0.2)
            continue
        break
    return n
