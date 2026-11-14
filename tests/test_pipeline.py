import json

from kom import jobs
from kom.config import Settings
from kom.db import connect
from kom.export import export_crawl
from kom.pipeline import drain, process_task, step
from kom.runtime import Runtime, build_runtime
from kom.urls import canonical_url, page_relpath


def make_settings(tmp_path, **kw):
    base = dict(
        db_path=str(tmp_path / "kom.sqlite"),
        out_dir=str(tmp_path / "knowledge"),
        site_dir=str(tmp_path / "site"),
        delay_s=0,
        backoff_s=0.01,
        lease_s=60,
        max_attempts=2,
    )
    base.update(kw)
    return Settings(**base)


def pages_by_url(conn, cid):
    return {r["url"]: dict(r) for r in conn.execute("SELECT * FROM pages WHERE crawl_id = ?", (cid,))}


def test_crawl_builds_hierarchy_files_graph_and_api(site, tmp_path):
    rt = build_runtime(make_settings(tmp_path))
    cid = jobs.create_crawl(rt.conn, site + "/", max_depth=4, max_pages=50)
    drain(rt, max_seconds=60)

    crawl = jobs.get_crawl(rt.conn, cid)
    assert crawl["status"] == "done" and crawl["stage"] == "done"

    pages = pages_by_url(rt.conn, cid)
    root, docs = pages[site + "/"], pages[site + "/docs"]
    guide, setup, intro = pages[site + "/docs/guide"], pages[site + "/docs/guide/setup"], pages[site + "/docs/intro"]

    # herança por path: setup -> guide -> docs -> raiz
    assert setup["parent_id"] == guide["id"]
    assert guide["parent_id"] == docs["id"]
    assert docs["parent_id"] == root["id"]
    assert root["parent_id"] is None
    assert intro["parent_id"] == docs["id"]

    # ordem topológica: pai antes do filho
    assert root["topo_index"] < docs["topo_index"] < guide["topo_index"] < setup["topo_index"]

    # fora do escopo e não-HTML não entram
    assert not any("external.example" in u for u in pages)
    assert not any(u.endswith(".png") for u in pages)

    # link quebrado vira 'dead' e não derruba o crawl
    dead = rt.conn.execute(
        "SELECT state FROM tasks WHERE crawl_id = ? AND url = ?", (cid, site + "/docs/missing")
    ).fetchone()
    assert dead["state"] == "dead"

    # arquivos Markdown espelham o path
    host_dir = site.split("//")[1].replace(":", "_")
    f = tmp_path / "knowledge" / host_dir / "docs" / "guide" / "setup" / "index.md"
    assert f.exists()
    text = f.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert f'parent: "{site}/docs/guide"' in text
    assert "(../../intro/index.md)" in text  # link interno reescrito para caminho relativo
    assert "Caminho:" in text
    guide_text = (tmp_path / "knowledge" / host_dir / "docs" / "guide" / "index.md").read_text(encoding="utf-8")
    assert "## Filhos" in guide_text and "(setup/index.md)" in guide_text
    assert pages[site + "/docs/guide/setup"]["file_path"] == page_relpath(site + "/docs/guide/setup")

    # grafo: pai, links e similaridade
    kinds = {r["kind"] for r in rt.conn.execute("SELECT kind FROM graph_edges WHERE crawl_id = ?", (cid,))}
    assert {"parent", "links_to", "similar"} <= kinds
    parent_edge = rt.conn.execute(
        "SELECT 1 FROM graph_edges WHERE kind='parent' AND src_id=? AND dst_id=?", (guide["id"], setup["id"])
    ).fetchone()
    assert parent_edge is not None

    # exportação estática
    doc = json.loads((tmp_path / "site" / "crawls" / f"{cid}.json").read_text(encoding="utf-8"))
    assert doc["tree"][0]["url"] == site + "/"
    assert (tmp_path / "site" / "index.json").exists()
    rt.close()


def test_max_depth_limits_recursion(site, tmp_path):
    rt = build_runtime(make_settings(tmp_path))
    cid = jobs.create_crawl(rt.conn, site + "/", max_depth=1, max_pages=50)
    drain(rt, max_seconds=60)
    urls = set(pages_by_url(rt.conn, cid))
    assert site + "/docs" in urls
    assert site + "/docs/intro" not in urls
    rt.close()


def test_max_pages_caps_the_frontier(site, tmp_path):
    rt = build_runtime(make_settings(tmp_path))
    cid = jobs.create_crawl(rt.conn, site + "/", max_depth=5, max_pages=2)
    drain(rt, max_seconds=60)
    assert rt.conn.execute("SELECT COUNT(*) FROM tasks WHERE crawl_id = ?", (cid,)).fetchone()[0] <= 2
    rt.close()


def test_expired_lease_is_reclaimed_after_worker_crash(site, tmp_path):
    rt = build_runtime(make_settings(tmp_path))
    cid = jobs.create_crawl(rt.conn, site + "/")
    # simula worker que pegou a tarefa e morreu: lease já vencido
    task = jobs.claim_task(rt.conn, lease_s=-1)
    assert task is not None and task["attempts"] == 1
    assert jobs.reclaim_expired(rt.conn) == 1
    again = jobs.claim_task(rt.conn, lease_s=60)
    assert again["id"] == task["id"] and again["attempts"] == 2
    process_task(rt, again)  # o worker substituto conclui a tarefa recuperada
    # o crawl completa normalmente depois da recuperação
    drain(rt, max_seconds=60)
    assert jobs.get_crawl(rt.conn, cid)["status"] == "done"
    rt.close()


class FlakyEnricher:
    name = "flaky"

    def __init__(self):
        self.calls = 0

    def similar_edges(self, pages):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("falha transitória no enriquecimento")
        return []


def test_failed_stage_is_retried_until_it_succeeds(site, tmp_path):
    s = make_settings(tmp_path)
    base = build_runtime(s)
    rt = Runtime(settings=s, conn=base.conn, fetcher=base.fetcher, converter=base.converter,
                 sink=base.sink, enricher=FlakyEnricher())
    cid = jobs.create_crawl(rt.conn, site + "/")
    drain(rt, max_seconds=60)
    types = [r["type"] for r in rt.conn.execute("SELECT type FROM events WHERE crawl_id = ? ORDER BY id", (cid,))]
    assert "stage.failed" in types
    assert types.index("stage.failed") < len(types) - 1
    assert jobs.get_crawl(rt.conn, cid)["status"] == "done"
    rt.close()


def test_crawl_gives_up_after_stage_max_attempts_and_can_resume(site, tmp_path):
    class AlwaysFail(FlakyEnricher):
        def similar_edges(self, pages):
            raise RuntimeError("fora do ar")

    s = make_settings(tmp_path, stage_max_attempts=2)
    base = build_runtime(s)
    rt = Runtime(settings=s, conn=base.conn, fetcher=base.fetcher, converter=base.converter,
                 sink=base.sink, enricher=AlwaysFail())
    cid = jobs.create_crawl(rt.conn, site + "/")
    drain(rt, max_seconds=60)
    assert jobs.get_crawl(rt.conn, cid)["status"] == "failed"

    # depois que o serviço volta, o operador retoma: nada é refeito do zero
    rt.enricher = FlakyEnricher()
    rt.enricher.calls = 1  # já passou da falha simulada
    jobs.resume_crawl(rt.conn, cid)
    drain(rt, max_seconds=60)
    assert jobs.get_crawl(rt.conn, cid)["status"] == "done"
    rt.close()


def test_persisted_state_survives_new_connection(site, tmp_path):
    s = make_settings(tmp_path)
    rt = build_runtime(s)
    cid = jobs.create_crawl(rt.conn, site + "/")
    step(rt)  # processa só a raiz
    rt.close()

    fresh = connect(s.db_path)
    assert fresh.execute("SELECT COUNT(*) FROM pages WHERE crawl_id = ?", (cid,)).fetchone()[0] == 1
    fresh.close()


def test_invalid_root_url_is_rejected(tmp_path):
    conn = connect(str(tmp_path / "x.sqlite"))
    try:
        jobs.create_crawl(conn, "ftp://nao-http.example/")
    except ValueError:
        pass
    else:
        raise AssertionError("deveria rejeitar URL não http(s)")
    assert canonical_url("ftp://nao-http.example/") is None
    conn.close()


def test_export_writes_json_api(site, tmp_path):
    rt = build_runtime(make_settings(tmp_path))
    cid = jobs.create_crawl(rt.conn, site + "/docs/guide")
    drain(rt, max_seconds=60)
    export_crawl(rt.conn, cid, str(tmp_path / "api"))
    pages = list((tmp_path / "api" / "crawls" / cid / "pages").glob("*.json"))
    assert pages, "deve exportar uma página por arquivo"
    rt.close()
