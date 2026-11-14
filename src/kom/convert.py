"""HTML -> Markdown. Remove ruído (scripts, navegação, rodapé), escolhe o conteúdo
principal e extrai os links (todos para descoberta; só do conteúdo para o grafo)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup
from markdownify import markdownify

from kom.factory import register
from kom.urls import canonical_url


@dataclass(frozen=True)
class Converted:
    title: str
    markdown: str
    links_all: list[str]  # para descobrir páginas (inclui navegação)
    links_content: list[str]  # para o grafo links_to (só conteúdo principal)


def _dedupe(seq: list[str]) -> list[str]:
    return list(dict.fromkeys(seq))


class MarkdownConverter:
    name = "markdownify"

    def convert(self, html: str, base_url: str) -> Converted:
        base = canonical_url(base_url) or base_url
        soup = BeautifulSoup(html, "html.parser")

        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        links_all = _dedupe(
            u for a in soup.find_all("a", href=True) if (u := canonical_url(a["href"], base)) and u != base
        )

        for tag in soup(["script", "style", "noscript", "template", "svg", "iframe", "form"]):
            tag.decompose()

        main = soup.find("main") or soup.find("article") or soup.body or soup
        for tag in main.find_all(["nav", "footer", "aside"]):
            tag.decompose()

        links_content = _dedupe(
            u for a in main.find_all("a", href=True) if (u := canonical_url(a["href"], base)) and u != base
        )
        if not title:
            h1 = main.find("h1")
            title = h1.get_text(" ", strip=True) if h1 else ""

        md = markdownify(str(main), heading_style="ATX")
        md = re.sub(r"\n{3,}", "\n\n", md).strip() + "\n"
        return Converted(title=title.strip(), markdown=md, links_all=links_all, links_content=links_content)


@register("converter", "markdownify")
def _make_converter(settings) -> MarkdownConverter:
    return MarkdownConverter()
