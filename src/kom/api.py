"""API FastAPI: CRUD sobre o banco, consultas de grafo, árvore, eventos e disparo de crawls.

Execução: `kom serve` ou `uvicorn kom.api:app`. Em hospedagem serverless, o worker
pode ser disparado por `POST /worker/tick` ou pelo workflow do GitHub Actions.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from kom import __version__, db, jobs
from kom.config import Settings
from kom.graph import build_tree
from kom.pipeline import run_steps, step
from kom.runtime import build_runtime
from kom.urls import canonical_url

log = logging.getLogger("kom")

INDEX_HTML = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><title>knowledge-org-in-md</title>
<style>body{font-family:system-ui;max-width:760px;margin:2rem auto;padding:0 1rem}
input,button{font:inherit;padding:.5rem}input{width:70%}pre{background:#f4f4f4;padding:1rem;overflow:auto}</style>
</head><body>
<h1>knowledge-org-in-md</h1>
<p>Informe a URL raiz. O sistema coleta as páginas recursivamente, herdando a hierarquia de paths.</p>
<form id="f">
  <input name="url" type="url" required placeholder="https://exemplo.com/docs/" />
  <input name="max_depth" type="number" value="2" min="0" max="10" style="width:4rem" />
  <button>Raspar</button>
</form>
<h3>Resposta</h3><pre id="out">—</pre>
<p><a href="/crawls">Lista de crawls (JSON)</a> · <a href="/docs">Documentação OpenAPI</a></p>
<script>
document.getElementById('f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const r = await fetch('/crawls', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({url: fd.get('url'), max_depth: Number(fd.get('max_depth'))})});
  document.getElementById('out').textContent = JSON.stringify(await r.json(), null, 2);
});
</script></body></html>"""


class CrawlIn(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    max_depth: int = Field(2, ge=0, le=10)
    max_pages: int = Field(200, ge=1, le=5000)


def _worker_loop(settings: Settings, stop: threading.Event) -> None:
    rt = build_runtime(settings)
    try:
        while not stop.is_set():
            if not step(rt):
                stop.wait(2.0)
    finally:
        rt.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        stop = threading.Event()
        if settings.inline_worker:
            threading.Thread(target=_worker_loop, args=(settings, stop), daemon=True).start()
        yield
        stop.set()

    app = FastAPI(title="knowledge-org-in-md", version=__version__, lifespan=lifespan)

    def get_conn() -> Iterator[Any]:
        conn = db.connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def crawl_or_404(conn: Any, crawl_id: str) -> dict[str, Any]:
        summary = jobs.crawl_summary(conn, crawl_id)
        if summary is None:
            raise HTTPException(404, "crawl não encontrado")
        return summary

    def page_or_404(conn: Any, page_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "página não encontrada")
        return dict(row)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return INDEX_HTML

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    # ---- crawls -------------------------------------------------------------
    @app.post("/crawls", status_code=201)
    def create_crawl(body: CrawlIn, conn: Any = Depends(get_conn)) -> dict[str, Any]:
        if canonical_url(body.url) is None:
            raise HTTPException(422, "URL inválida: use http ou https")
        crawl_id = jobs.create_crawl(conn, body.url, body.max_depth, body.max_pages)
        return crawl_or_404(conn, crawl_id)

    @app.get("/crawls")
    def list_crawls(limit: int = Query(50, ge=1, le=500), conn: Any = Depends(get_conn)) -> list[dict[str, Any]]:
        rows = conn.execute("SELECT * FROM crawls ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    @app.get("/crawls/{crawl_id}")
    def get_crawl(crawl_id: str, conn: Any = Depends(get_conn)) -> dict[str, Any]:
        out = crawl_or_404(conn, crawl_id)
        out["recent_events"] = [dict(r) for r in conn.execute(
            "SELECT type, payload, created_at FROM events WHERE crawl_id = ? ORDER BY id DESC LIMIT 20",
            (crawl_id,),
        )]
        return out

    @app.post("/crawls/{crawl_id}/resume")
    def resume(crawl_id: str, conn: Any = Depends(get_conn)) -> dict[str, Any]:
        crawl_or_404(conn, crawl_id)
        jobs.resume_crawl(conn, crawl_id)
        return crawl_or_404(conn, crawl_id)

    # ---- worker -------------------------------------------------------------
    @app.post("/worker/tick")
    def tick(max_steps: int = Query(20, ge=1, le=500)) -> dict[str, int]:
        rt = build_runtime(settings)
        try:
            return {"steps": run_steps(rt, max_steps)}
        finally:
            rt.close()

    # ---- páginas ------------------------------------------------------------
    @app.get("/crawls/{crawl_id}/pages")
    def list_pages(
        crawl_id: str,
        parent_id: str | None = None,
        q: str | None = Query(None, description="busca em título e URL"),
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        conn: Any = Depends(get_conn),
    ) -> list[dict[str, Any]]:
        crawl_or_404(conn, crawl_id)
        sql = ("SELECT id, url, title, depth, parent_id, topo_index, file_path, fetched_at "
               "FROM pages WHERE crawl_id = ?")
        args: list[Any] = [crawl_id]
        if parent_id is not None:
            sql += " AND parent_id = ?"
            args.append(parent_id)
        if q:
            sql += " AND (title LIKE ? OR url LIKE ?)"
            args += [f"%{q}%", f"%{q}%"]
        sql += " ORDER BY topo_index, url LIMIT ? OFFSET ?"
        args += [limit, offset]
        return [dict(r) for r in conn.execute(sql, args)]

    @app.get("/pages/{page_id}")
    def get_page(page_id: str, conn: Any = Depends(get_conn)) -> dict[str, Any]:
        return page_or_404(conn, page_id)

    @app.get("/pages/{page_id}/children")
    def children(page_id: str, conn: Any = Depends(get_conn)) -> list[dict[str, Any]]:
        page_or_404(conn, page_id)
        rows = conn.execute(
            "SELECT id, url, title, depth, topo_index FROM pages WHERE parent_id = ? ORDER BY url",
            (page_id,),
        )
        return [dict(r) for r in rows]

    @app.get("/pages/{page_id}/ancestors")
    def ancestors(page_id: str, conn: Any = Depends(get_conn)) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cur = page_or_404(conn, page_id)
        while cur["parent_id"]:
            cur = page_or_404(conn, cur["parent_id"])
            out.append({"id": cur["id"], "url": cur["url"], "title": cur["title"], "depth": cur["depth"]})
        return out

    # ---- grafo --------------------------------------------------------------
    @app.get("/crawls/{crawl_id}/graph")
    def graph(
        crawl_id: str,
        kind: str | None = Query(None, pattern="^(parent|links_to|similar)$"),
        conn: Any = Depends(get_conn),
    ) -> dict[str, Any]:
        crawl_or_404(conn, crawl_id)
        nodes = [dict(r) for r in conn.execute(
            "SELECT id, url, title, depth, parent_id, topo_index FROM pages WHERE crawl_id = ?", (crawl_id,))]
        sql = ("SELECT src_id AS source, dst_id AS target, kind, weight FROM graph_edges WHERE crawl_id = ?")
        args: list[Any] = [crawl_id]
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        edges = [dict(r) for r in conn.execute(sql, args)]
        return {"nodes": nodes, "edges": edges}

    @app.get("/crawls/{crawl_id}/topo")
    def topo(crawl_id: str, conn: Any = Depends(get_conn)) -> list[dict[str, Any]]:
        crawl_or_404(conn, crawl_id)
        return [dict(r) for r in conn.execute(
            "SELECT id, url, depth, topo_index, parent_id FROM pages WHERE crawl_id = ? "
            "AND topo_index IS NOT NULL ORDER BY topo_index", (crawl_id,))]

    @app.get("/crawls/{crawl_id}/tree")
    def tree(crawl_id: str, conn: Any = Depends(get_conn)) -> list[dict[str, Any]]:
        crawl_or_404(conn, crawl_id)
        pages = [dict(r) for r in conn.execute(
            "SELECT id, url, title, depth, parent_id FROM pages WHERE crawl_id = ? ORDER BY depth, url",
            (crawl_id,))]
        return build_tree(pages)

    # ---- eventos ------------------------------------------------------------
    @app.get("/events")
    def events(
        crawl_id: str | None = None,
        limit: int = Query(100, ge=1, le=1000),
        conn: Any = Depends(get_conn),
    ) -> list[dict[str, Any]]:
        if crawl_id:
            rows = conn.execute("SELECT * FROM events WHERE crawl_id = ? ORDER BY id DESC LIMIT ?",
                                (crawl_id, limit))
        else:
            rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    return app


app = create_app()  # uvicorn kom.api:app
