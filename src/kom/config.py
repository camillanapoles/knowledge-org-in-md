"""Configuração do sistema. Tudo pode ser sobrescrito por variável de ambiente (KOM_*)."""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_UA = "knowledge-org-in-md/0.1 (+https://github.com/camillanapoles/knowledge-org-in-md)"


@dataclass(frozen=True)
class Settings:
    db_path: str = "data/kom.sqlite"
    out_dir: str = "knowledge"  # Markdown espelhando a hierarquia de paths
    site_dir: str = "site/api"  # JSON estático (API para GitHub Pages)
    user_agent: str = DEFAULT_UA
    delay_s: float = 0.5  # intervalo mínimo entre requisições (educação de crawler)
    timeout_s: float = 20.0
    lease_s: float = 300.0  # tempo que um worker tem para concluir uma tarefa
    max_attempts: int = 3  # tentativas por página antes de marcar 'dead'
    backoff_s: float = 2.0  # backoff exponencial base entre tentativas
    stage_max_attempts: int = 5  # tentativas por estágio (graph/store/export) antes de 'failed'
    fetcher: str = "http"
    converter: str = "markdownify"
    sink: str = "filesystem"
    enricher: str = "tfidf"
    similarity_threshold: float = 0.15
    inline_worker: bool = False  # roda o worker em thread dentro da API (dev)

    @classmethod
    def from_env(cls, **overrides: object) -> "Settings":
        e = os.environ
        values: dict[str, object] = {
            "db_path": e.get("KOM_DB", cls.db_path),
            "out_dir": e.get("KOM_OUT", cls.out_dir),
            "site_dir": e.get("KOM_SITE", cls.site_dir),
            "user_agent": e.get("KOM_USER_AGENT", DEFAULT_UA),
            "delay_s": float(e.get("KOM_DELAY", "0.5")),
            "enricher": e.get("KOM_ENRICHER", cls.enricher),
            "inline_worker": e.get("KOM_INLINE_WORKER", "0") == "1",
        }
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)  # type: ignore[arg-type]
