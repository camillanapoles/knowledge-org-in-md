# ADR 0001 — Repositório de sistema × repositório "knowledge only"

Status: **aceito** · Data: 2026-11-14

## Contexto

O projeto gera dois tipos de coisa com ciclos de vida diferentes:

1. **Código do sistema**: scraper, conversor, grafo, fila, API FastAPI, workflows de CI.
   Muda por commit de desenvolvimento, precisa de revisão, testes e versionamento semântico.
2. **Conhecimento gerado**: páginas em Markdown espelhando a hierarquia de paths do site raspado,
   JSON do grafo e da API estática. Muda a cada execução de crawl, pode gerar milhares de arquivos
   por dia e tem permissões e visibilidade próprias.

## Decisão

Separar em **dois repositórios**:

| Repositório | Papel | Conteúdo |
|---|---|---|
| `knowledge-org-in-md` (este) | **sistema** | `src/`, `tests/`, `.github/workflows/`, `docs/` |
| repositório de conhecimento (`KNOWLEDGE_REPO`, configurável) | **knowledge only** | `knowledge/<host>/<path>/index.md`, `api/` (JSON servido por GitHub Pages) |

O workflow `crawl.yml` executa o pipeline neste repositório e faz push do resultado para o
repositório de conhecimento. Se a variável `KNOWLEDGE_REPO` não estiver definida, o resultado
vai apenas para o artefato do workflow.

## Justificativa

- **Histórico limpo**: diffs de crawl (milhares de linhas de Markdown) não poluem o histórico de código.
- **Ciclos independentes**: o código pode ser revisado/testado sem depender de um crawl, e
  o conhecimento pode ser republicado sem nova release do sistema.
- **Permissões**: o repositório de conhecimento pode ser público (Pages) enquanto o sistema
  fica restrito, ou vice-versa; tokens têm escopo mínimo (`KNOWLEDGE_REPO_TOKEN` só escreve no KB).
- **Sem loops de CI**: pushes no repositório de conhecimento não disparam o CI do sistema.
- **Reprodutibilidade**: o conhecimento registra o hash/versão do sistema que o gerou (campo
  `kom_version`, a adicionar no front matter) — o sistema é a "função", o KB é a "saída".
- **Limites de tamanho**: o banco SQLite **não** é versionado em Git; ele vive em `actions/cache`
  (ver README). Isso evita binário crescente no histórico.

## Alternativa rejeitada

Um único repositório *monorepo* com `knowledge/` versionado. Simples para começar, mas mistura
os ciclos acima e faz o histórico de código crescer com cada crawl. Só faria sentido para um
projeto pessoal de baixo volume — e mesmo assim o `.gitignore` já deixa `knowledge/` local.

## Consequências

- Dois repositórios a manter; a configuração fica em `vars.KNOWLEDGE_REPO` + `secrets.KNOWLEDGE_REPO_TOKEN`.
- O repositório de conhecimento precisa habilitar GitHub Pages (pasta `/` da branch principal) para
  expor `api/index.json` como API estática.
