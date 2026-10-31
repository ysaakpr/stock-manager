"""HTML to readable text for the direct fetcher, on the standard library's parser.

Keeps the words a reader sees and the paragraph breaks between blocks; drops scripts, styles,
navigation, forms, SVG and everything inside `<head>` except the title. Not a readability engine:
the vendors (Firecrawl, Tavily) do main-content extraction better, which is why direct fetch is
the last fetch fallback rather than the first.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Final

__all__ = ["html_to_text"]

_SKIP: Final[frozenset[str]] = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "svg",
        "canvas",
        "iframe",
        "nav",
        "form",
        "button",
        "select",
        "head",
    }
)
_BLOCK: Final[frozenset[str]] = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "main",
        "header",
        "footer",
        "aside",
        "br",
        "hr",
        "li",
        "ul",
        "ol",
        "table",
        "tr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "pre",
        "dt",
        "dd",
        "figcaption",
    }
)
_VOID: Final[frozenset[str]] = frozenset(
    {"br", "hr", "img", "meta", "link", "input", "area", "base", "col", "source", "wbr"}
)


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
            return
        if tag in _SKIP and tag not in _VOID:
            self._skip_depth += 1
            return
        if tag in _BLOCK:
            self.parts.append("\n")
        if tag in {"td", "th"}:
            self.parts.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
            return
        if tag in _SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        elif self._skip_depth == 0:
            self.parts.append(data)


_SPACES = re.compile(r"[ \t\r\f\v\u00a0]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def html_to_text(markup: str) -> tuple[str, str]:
    """`(title, text)` of an HTML document: visible text, blocks on their own lines."""
    parser = _Extractor()
    parser.feed(markup)
    parser.close()
    lines = [_SPACES.sub(" ", line).strip() for line in "".join(parser.parts).split("\n")]
    text = _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()
    title = " ".join(html.unescape("".join(parser.title_parts)).split())
    return title, text
