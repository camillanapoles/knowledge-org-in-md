# knowledge-org-in-md (`kom`)

Recebe uma **URL raiz**, raspa as páginas recursivamente respeitando a **herança por path**
(`/docs/guide/setup` é filho de `/docs/guide`, que é filho de `/docs`, que é filho da raiz),
converte cada página para **Markdown**, armazena tudo em **SQLite** com IDs únicos e monta um
**grafo de conhecimento** com arestas de parentesco, links e similaridade semântica.

Este repositório é o **sistema** (código, pipeline, CI). O conhecimento gerado vai para um
**repositório separado** — a decisão e o raciocínio estão em
[`docs/adr/0001-sistema-vs-repositorio-de-conhecimento.md`](docs/adr/0001-sistema-vs-repositorio-de-conhecimento.md).

## Arquitetura

```
SISTEMA
├── raspagem        fila durável (tarefas com lease) → fetch educado (robots.txt, delay, timeout)
├── conversão       HTML → Markdown (remove nav/rodapé; extrai links do conteúdo e de toda a página)
├── grafo           arestas: parent (herança por path) · links_to (hyperlinks) · similar (TF-IDF)
│                   ordem topológica (pai antes do filho) via graphlib
├── armazenamento   SQLite relacional (crawls, tasks, pages, links, graph_edges, events)
│                   Markdown espelhando o path: host/docs/guide/setup/index.md
├── eventos         tabela `events` (trilha durável) + retentativas por estágio
└── API             FastAPI: CRUD, consultas, árvore, grafo, topo, eventos, disparo de worker
```

### Pipeline e recuperação

Cada crawl passa por `scrape → graph → store → export → done`.

| Falha | O que acontece |
|---|---|
| Queda do worker no meio de uma página | O lease expira; `reclaim_expired` devolve a tarefa à fila. A gravação da página e a conclusão da tarefa são **uma transação**, então nada fica pela metade. |
| Erro transitório de rede ou 5xx | Retentativa com backoff exponencial até `max_attempts`; depois a tarefa vira `dead`. |
| 404/410 | `dead` imediatamente, sem retentativa. |
| Falha em `graph`/`store`/`export` | `stage_attempts` sobe e o estágio é refeito (todos são idempotentes). Após `stage_max_attempts` o crawl vira `failed`. |
| Crawl `failed` ou execução anterior interrompida | `kom resume <id>` ou `kom worker` retoma. Nada é refeito do zero. |

### Herança por path (org-mode)

- O pai de uma página é o **ancestral mais próximo que foi de fato coletado**. Se `/docs/guide`
  não estiver no crawl, `/docs/guide/setup` herda de `/docs`.
- O escopo é o **prefixo do path raiz**: com raiz `https://x.com/docs`, `/docs/a` entra e
  `/docsnovo` não entra.
- Cada página é uma pasta com `index.md`, então os filhos ficam aninhados dentro dela. O Markdown
  tem breadcrumb, lista de filhos e **links internos reescritos** para caminhos relativos.

## Uso local

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q

kom crawl --url https://exemplo.com/docs/ --max-depth 2   # cria e executa
kom status                                                  # lista crawls
kom status <crawl_id>                                       # detalhe
kom resume <crawl_id>                                       # retoma um crawl falho
kom export                                                  # regenera a API estática
kom serve --port 8000                                       # API em http://localhost:8000
```

Variáveis: `KOM_DB` (padrão `data/kom.sqlite`), `KOM_OUT` (`knowledge`), `KOM_SITE` (`site/api`),
`KOM_DELAY` (segundos entre requisições, padrão `0.5`), `KOM_USER_AGENT`, `KOM_INLINE_WORKER=1`.

## API (FastAPI)

| Método | Rota | Função |
|---|---|---|
| GET | `/` | página com input de URL |
| POST | `/crawls` | cria crawl `{url, max_depth, max_pages}` |
| GET | `/crawls`, `/crawls/{id}` | lista e detalhe (contagens, eventos recentes) |
| POST | `/crawls/{id}/resume` | retoma crawl falho |
| POST | `/worker/tick?max_steps=` | processa parte da fila (serverless / cron) |
| GET | `/crawls/{id}/pages` | páginas (filtros: `parent_id`, `q`) |
| GET | `/pages/{id}` | página com Markdown |
| GET | `/pages/{id}/children`, `/ancestors` | navegação da herança |
| GET | `/crawls/{id}/graph?kind=` | nós e arestas (`parent`, `links_to`, `similar`) |
| GET | `/crawls/{id}/topo` | ordem topológica |
| GET | `/crawls/{id}/tree` | árvore aninhada |
| GET | `/events?crawl_id=` | log de eventos |

Documentação interativa em `/docs`.

## GitHub Actions: parte serverless

- **`ci.yml`**: roda os testes em cada push e PR.
- **`crawl.yml`** (`workflow_dispatch`, com `url`, `max_depth`, `max_pages`):
  1. restaura o SQLite do cache da execução anterior;
  2. `kom worker` retoma pendências de execuções que caíram;
  3. executa o novo crawl;
  4. salva o SQLite no cache (mesmo se o crawl falhar, para retomada);
  5. publica Markdown e JSON no repositório de conhecimento, se `vars.KNOWLEDGE_REPO` e
     `secrets.KNOWLEDGE_REPO_TOKEN` estiverem definidos; caso contrário, deixa o artefato.

**Configuração necessária:**

- `vars.KNOWLEDGE_REPO` = `dono/nome-do-repo-de-conhecimento`
- `secrets.KNOWLEDGE_REPO_TOKEN` = token com permissão de escrita **só** nesse repositório
- No repositório de conhecimento, ative **GitHub Pages** (branch principal, pasta `/`). A pasta
  `api/` vira uma API somente leitura em `https://<dono>.github.io/<repo>/api/index.json`.

### Sobre "API no GitHub" e "banco serverless"

- Um GitHub Action **não** mantém um servidor no ar. Por isso o sistema separa dois modos:
  - **Escrita e processamento** → Actions (`crawl.yml`), com SQLite no cache do Actions.
  - **Leitura pública** → JSON estático em GitHub Pages (`api/`).
  - **Consultas dinâmicas** (filtros, grafo sob demanda) → `kom serve` (FastAPI), que você
    hospeda em qualquer container. O código não depende de um host específico.
- O SQLite não é versionado no Git (binário que cresce a cada commit). Ele vive em
  `actions/cache`. Limitação: o cache expira em 7 dias sem uso, e a API dinâmica precisa de uma
  cópia do `.sqlite` (pode ser baixada do cache ou do artefato).

## Grafo semântico: o que está pronto e o que não está

- **Pronto**: arestas `parent`, `links_to` e `similar` (TF-IDF + cosseno, determinístico, sem chave
  de API, roda no Actions).
- **Não incluído nesta versão**: um enriquecedor com embeddings ou LLM (estilo GraphRAG/graphify).
  A arquitetura já prevê isso: a fábrica (`kom/factory.py`) aceita um novo enriquecedor com o
  método `similar_edges`, e o workflow pode receber a chave do provedor como secret. Não é possível
  garantir qualidade "SOTA 2026" sem avaliar um modelo específico, então deixei isso como próximo
  passo explícito.
- **Grafo global**: cada crawl tem seu grafo. O conhecimento de um repositório inteiro é a união dos
  crawls, que se organizam na árvore de pastas do próprio repositório de conhecimento.

## Estrutura

```
src/kom/
  config.py  db.py  urls.py  factory.py  runtime.py
  jobs.py        fila durável (claim, lease, reclaim)
  pipeline.py    passos do pipeline + gestor de eventos + retentativas
  scraper.py     fetcher HTTP (robots, delay)   [fábrica: fetcher]
  convert.py     HTML → Markdown                [fábrica: converter]
  graph.py       herança, topo, arestas
  enrich.py      similaridade TF-IDF            [fábrica: enricher]
  store.py       Markdown espelhando o path     [fábrica: sink]
  export.py      API estática (JSON)
  api.py         FastAPI
  cli.py         comando `kom`
tests/           testes com site local (herança, falhas, retomada, API)
docs/adr/        decisões de arquitetura
.github/workflows/  ci.yml, crawl.yml
```
