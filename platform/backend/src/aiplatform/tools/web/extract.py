"""HTML → readable text (title, main text, links). Output is plain text; nothing is ever rendered as HTML."""

from __future__ import annotations

import contextlib
import json
import re
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

_DROP = ["script", "style", "noscript", "svg", "canvas", "iframe", "template", "form", "nav", "footer", "header", "aside"]
_BLANKS = re.compile(r"\n\s*\n+")
_SPACES = re.compile(r"[ \t ]+")


def html_to_text(html: str, base_url: str, max_chars: int = 20000) -> dict[str, object]:
    tree = HTMLParser(html)
    title_node = tree.css_first("title")
    title = title_node.text(strip=True)[:300] if title_node is not None else ""
    links = []
    for a in tree.css("a[href]")[:400]:
        href = (a.attributes.get("href") or "").strip()
        text = a.text(strip=True)[:100]
        if href and not href.startswith(("javascript:", "mailto:", "#", "data:")) and text:
            links.append({"text": text, "url": urljoin(base_url, href)})
        if len(links) >= 40:
            break
    for sel in _DROP:
        for n in tree.css(sel):
            n.decompose()
    root = tree.css_first("main") or tree.css_first("article") or tree.body or tree.root
    text = root.text(separator="\n") if root is not None else ""
    text = _BLANKS.sub("\n\n", "\n".join(_SPACES.sub(" ", ln).strip() for ln in text.splitlines())).strip()
    return {"title": title, "text": text[:max_chars], "truncated": len(text) > max_chars, "links": links}


def body_to_text(body: bytes, content_type: str, base_url: str, max_chars: int) -> dict[str, object]:
    raw = body.decode("utf-8", errors="replace")
    if content_type in ("text/html", "application/xhtml+xml") or (not content_type and "<html" in raw[:2000].lower()):
        return html_to_text(raw, base_url, max_chars)
    if content_type == "application/json":
        with contextlib.suppress(ValueError):
            raw = json.dumps(json.loads(raw), indent=1, ensure_ascii=False)
    return {"title": "", "text": raw[:max_chars], "truncated": len(raw) > max_chars, "links": []}
