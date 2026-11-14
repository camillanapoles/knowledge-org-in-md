import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

PAGES = {
    "/": (
        '<html><head><title>Início</title></head><body>'
        '<nav><a href="/sobre">Sobre</a></nav>'
        '<main><h1>Início</h1><p>Grafo de conhecimento com FastAPI e SQLite.</p>'
        '<a href="/docs/">Documentação</a>'
        '<a href="https://external.example/x">externo</a>'
        '<a href="/logo.png">logo</a></main>'
        '<footer><a href="/docs/guide/setup">setup</a></footer></body></html>'
    ),
    "/sobre": (
        '<html><head><title>Sobre</title></head><body><main><h1>Sobre</h1>'
        '<p>Projeto de raspagem recursiva de documentação.</p></main></body></html>'
    ),
    "/docs": (
        '<html><head><title>Docs</title></head><body><main><h1>Docs</h1>'
        '<a href="/docs/intro">Intro</a><a href="/docs/guide">Guia</a>'
        '<a href="/docs/missing">Quebrado</a></main></body></html>'
    ),
    "/docs/intro": (
        '<html><head><title>Intro</title></head><body><main><h1>Intro</h1>'
        '<p>Knowledge graph semantic organization topological ordering.</p></main></body></html>'
    ),
    "/docs/guide": (
        '<html><head><title>Guia</title></head><body><main><h1>Guia</h1>'
        '<a href="/docs/guide/setup">Setup</a></main></body></html>'
    ),
    "/docs/guide/setup": (
        '<html><head><title>Setup</title></head><body><main><h1>Setup</h1>'
        '<p>Instalação FastAPI SQLite graph knowledge.</p>'
        '<a href="/docs/intro">Intro</a></main></body></html>'
    ),
}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"
        body = PAGES.get(path)
        if body is None:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *args):  # silencia logs do servidor de teste
        pass


@pytest.fixture
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
