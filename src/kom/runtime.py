"""Runtime: agrupa conexão e componentes escolhidos pela fábrica."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from kom import convert, db, enrich, scraper, store  # noqa: F401  (registram as fábricas)
from kom.config import Settings
from kom.factory import create


@dataclass
class Runtime:
    settings: Settings
    conn: sqlite3.Connection
    fetcher: Any
    converter: Any
    sink: Any
    enricher: Any

    def close(self) -> None:
        close = getattr(self.fetcher, "close", None)
        if close:
            close()
        self.conn.close()


def build_runtime(settings: Settings) -> Runtime:
    return Runtime(
        settings=settings,
        conn=db.connect(settings.db_path),
        fetcher=create("fetcher", settings.fetcher, settings),
        converter=create("converter", settings.converter, settings),
        sink=create("sink", settings.sink, settings),
        enricher=create("enricher", settings.enricher, settings),
    )
