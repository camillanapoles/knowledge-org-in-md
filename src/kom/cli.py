"""CLI `kom`.

  kom crawl --url https://exemplo.com/docs/ --max-depth 2
  kom worker --drain            # retoma tudo que ficou pendente (após queda)
  kom status [CRAWL_ID]
  kom resume CRAWL_ID
  kom export
  kom serve --port 8000
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from kom import jobs
from kom.config import Settings
from kom.export import export_crawl
from kom.pipeline import drain
from kom.runtime import build_runtime


def _common() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--db", help="caminho do SQLite (padrão: KOM_DB ou data/kom.sqlite)")
    p.add_argument("--out", help="pasta do Markdown (padrão: KOM_OUT ou knowledge)")
    p.add_argument("--site", help="pasta da API estática (padrão: KOM_SITE ou site/api)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    common = _common()
    parser = argparse.ArgumentParser(prog="kom", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("crawl", parents=[common], help="cria um crawl e executa até o fim")
    c.add_argument("--url", required=True)
    c.add_argument("--max-depth", type=int, default=2)
    c.add_argument("--max-pages", type=int, default=200)
    c.add_argument("--no-drain", action="store_true", help="só enfileira")

    w = sub.add_parser("worker", parents=[common], help="processa a fila")

    s = sub.add_parser("status", parents=[common], help="mostra crawls ou um crawl")
    s.add_argument("crawl_id", nargs="?")

    r = sub.add_parser("resume", parents=[common], help="reabre um crawl falho")
    r.add_argument("crawl_id")

    sub.add_parser("export", parents=[common], help="exporta a API estática para o crawl mais recente")

    sv = sub.add_parser("serve", parents=[common], help="sobe a API FastAPI")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--worker", action="store_true", help="roda o worker em thread dentro da API")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if getattr(args, "verbose", False) else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env(db_path=args.db, out_dir=args.out, site_dir=args.site)

    if args.cmd == "serve":
        import uvicorn

        from kom.api import create_app

        app = create_app(Settings.from_env(db_path=args.db, out_dir=args.out, site_dir=args.site,
                                           inline_worker=args.worker or None))
        uvicorn.run(app, host=args.host, port=args.port)
        return 0

    rt = build_runtime(settings)
    try:
        if args.cmd == "crawl":
            crawl_id = jobs.create_crawl(rt.conn, args.url, args.max_depth, args.max_pages)
            print(f"crawl {crawl_id} criado", file=sys.stderr)
            if not args.no_drain:
                drain(rt)
            return _print_status(rt, crawl_id)
        if args.cmd == "worker":
            n = drain(rt)
            print(json.dumps({"steps": n}))
            return 0
        if args.cmd == "status":
            if args.crawl_id:
                return _print_status(rt, args.crawl_id)
            rows = [dict(r) for r in rt.conn.execute(
                "SELECT id, root_url, status, stage, created_at FROM crawls ORDER BY created_at DESC")]
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        if args.cmd == "resume":
            jobs.resume_crawl(rt.conn, args.crawl_id)
            drain(rt)
            return _print_status(rt, args.crawl_id)
        if args.cmd == "export":
            row = rt.conn.execute("SELECT id FROM crawls ORDER BY created_at DESC LIMIT 1").fetchone()
            if row is None:
                print("nenhum crawl", file=sys.stderr)
                return 1
            export_crawl(rt.conn, row["id"], settings.site_dir)
            print(f"exportado em {settings.site_dir}")
            return 0
    finally:
        rt.close()
    return 1


def _print_status(rt, crawl_id: str) -> int:
    summary = jobs.crawl_summary(rt.conn, crawl_id)
    if summary is None:
        print(f"crawl {crawl_id} não encontrado", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0 if summary["status"] == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
