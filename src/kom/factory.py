"""Registro de fábricas (Factory pattern).

Cada componente plugável (fetcher, converter, sink, enricher) registra uma
implementação com um nome. O runtime escolhe a implementação pela configuração,
então trocar HTTP por Playwright, ou TF-IDF por embeddings/LLM, não exige
mudar o pipeline.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kom.config import Settings

_REGISTRY: dict[str, dict[str, Callable[[Settings], Any]]] = {}


def register(kind: str, name: str) -> Callable[[Callable[[Settings], Any]], Callable[[Settings], Any]]:
    def deco(factory: Callable[[Settings], Any]) -> Callable[[Settings], Any]:
        _REGISTRY.setdefault(kind, {})[name] = factory
        return factory

    return deco


def create(kind: str, name: str, settings: Settings) -> Any:
    options = _REGISTRY.get(kind, {})
    if name not in options:
        raise ValueError(f"{kind} desconhecido: {name!r}. Opções: {sorted(options)}")
    return options[name](settings)


def available(kind: str) -> list[str]:
    return sorted(_REGISTRY.get(kind, {}))
