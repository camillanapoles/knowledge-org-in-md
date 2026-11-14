"""Enriquecimento semântico: arestas 'similar' entre páginas.

Implementação padrão: TF-IDF + similaridade de cosseno, sem dependências pesadas e
determinística (roda dentro do GitHub Action sem GPU nem chave de API).

Para embeddings ou um LLM (ex.: extração de entidades no workflow), basta registrar
outro enricher na fábrica com o mesmo método `similar_edges`.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable

from kom.factory import register

TOKEN = re.compile(r"\w{3,}")
STOPWORDS = frozenset(
    """a o as os e de da do das dos em um uma para por com que se na no nas nos é ao aos
    the and of to in is for on with this that as at by an be or from it are was were
    não mais também como ou sua seu isso esse essa este esta são foi ser têm tem
    """.split()
)
MAX_DF_RATIO = 0.5  # termos presentes em mais de metade das páginas não diferenciam nada
TOP_K = 3


class TfidfEnricher:
    name = "tfidf"

    def __init__(self, threshold: float = 0.15) -> None:
        self.threshold = threshold

    def similar_edges(self, pages: Iterable[dict]) -> list[tuple[str, str, float]]:
        docs: dict[str, Counter] = {}
        for p in pages:
            tokens = [t for t in TOKEN.findall(f"{p['title']} {p['markdown']}".lower())
                      if t not in STOPWORDS and not t.isdigit()]
            docs[p["id"]] = Counter(tokens)
        n = len(docs)
        if n < 2:
            return []

        df: Counter = Counter()
        for c in docs.values():
            df.update(c.keys())
        idf = {t: math.log((1 + n) / (1 + d)) + 1.0 for t, d in df.items() if d / n <= MAX_DF_RATIO}

        vecs: dict[str, dict[str, float]] = {}
        for pid, counts in docs.items():
            w = {t: tf * idf[t] for t, tf in counts.items() if t in idf}
            norm = math.sqrt(sum(v * v for v in w.values()))
            if norm:
                vecs[pid] = {t: v / norm for t, v in w.items()}

        # índice invertido: só compara pares que compartilham algum termo
        inv: dict[str, list[str]] = defaultdict(list)
        for pid, v in vecs.items():
            for t in v:
                inv[t].append(pid)
        sims: dict[str, dict[str, float]] = defaultdict(dict)
        for t, plist in inv.items():
            for i, a in enumerate(plist):
                for b in plist[i + 1:]:
                    sims[a][b] = sims[a].get(b, 0.0) + vecs[a][t] * vecs[b][t]
                    sims[b][a] = sims[a][b]

        keep: set[tuple[str, str]] = set()
        for a, nbrs in sims.items():
            best = sorted(nbrs.items(), key=lambda kv: kv[1], reverse=True)[:TOP_K]
            for b, s in best:
                if s >= self.threshold:
                    keep.add((min(a, b), max(a, b)))

        out = []
        for a, b in sorted(keep):
            out.append((a, b, round(sims[a][b], 4)))
        return out


class NoopEnricher:
    name = "none"

    def similar_edges(self, pages: Iterable[dict]) -> list[tuple[str, str, float]]:
        return []


@register("enricher", "tfidf")
def _make_tfidf(settings) -> TfidfEnricher:
    return TfidfEnricher(settings.similarity_threshold)


@register("enricher", "none")
def _make_none(settings) -> NoopEnricher:
    return NoopEnricher()
