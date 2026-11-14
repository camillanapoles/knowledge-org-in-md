from fastapi.testclient import TestClient

from kom.api import create_app
from tests.test_pipeline import make_settings


def test_api_end_to_end(site, tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as c:
        assert c.get("/health").json()["status"] == "ok"
        assert "<form" in c.get("/").text

        bad = c.post("/crawls", json={"url": "ftp://x.example/algo"})
        assert bad.status_code == 422

        r = c.post("/crawls", json={"url": site + "/", "max_depth": 4, "max_pages": 50})
        assert r.status_code == 201
        cid = r.json()["id"]

        assert c.post("/worker/tick", params={"max_steps": 500}).status_code == 200
        crawl = c.get(f"/crawls/{cid}").json()
        assert crawl["status"] == "done" and crawl["stage"] == "done"
        assert crawl["pages"] >= 5
        assert any(e["type"] == "stage.completed" for e in crawl["recent_events"])

        pages = c.get(f"/crawls/{cid}/pages").json()
        by_url = {p["url"]: p for p in pages}
        setup = by_url[site + "/docs/guide/setup"]

        anc = c.get(f"/pages/{setup['id']}/ancestors").json()
        assert [a["url"] for a in anc] == [site + "/docs/guide", site + "/docs", site + "/"]

        kids = c.get(f"/pages/{by_url[site + '/docs']['id']}/children").json()
        assert {k["url"] for k in kids} >= {site + "/docs/intro", site + "/docs/guide"}

        full = c.get(f"/pages/{setup['id']}").json()
        assert "FastAPI" in full["markdown"]

        graph = c.get(f"/crawls/{cid}/graph", params={"kind": "parent"}).json()
        assert graph["edges"] and all(e["kind"] == "parent" for e in graph["edges"])

        topo = c.get(f"/crawls/{cid}/topo").json()
        assert topo[0]["url"] == site + "/"

        tree = c.get(f"/crawls/{cid}/tree").json()
        assert len(tree) == 1 and tree[0]["url"] == site + "/"

        assert c.get("/pages/pg_inexistente").status_code == 404
        assert c.get("/crawls/cr_inexistente").status_code == 404
        assert c.get("/events", params={"crawl_id": cid}).json()
        assert c.get("/crawls").json()[0]["id"] == cid
