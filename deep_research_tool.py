"""
deep_research_tool.py
=====================
Senior FDE artifact — Deep Research Agent data-gathering tool for Dify orchestrators.

Design goals
------------
• Dual-source: tries Google News RSS first, falls back to DuckDuckGo HTML scrape.
• Hard token budget: compressed output is capped at MAX_OUTPUT_TOKENS characters
  (≈ tokens at ~4 chars/token) so the Dify orchestrator never blows its 2 000-token
  session limit.
• Zero paid APIs: fully self-contained, no API keys required.
• Dify-ready: exposes a single entry-point `run(query)` that returns a plain-text
  string, making it trivial to wrap as a Dify Tool / Code node.

Usage (standalone)
------------------
    python deep_research_tool.py "Impact of US tariffs on Pakistan textile exports 2025"

Usage (Dify Code node)
----------------------
    # Paste the entire file into a Dify "Code" node, then call:
    result = run(query)           # query comes from Dify's workflow variable
    return {"summary": result}
"""

from __future__ import annotations

import re
import sys
import textwrap
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from typing import NamedTuple

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG — tune these to fit your session budget
# ──────────────────────────────────────────────────────────────────────────────

TOP_N_RESULTS      = 3          # Number of results to fetch
MAX_OUTPUT_CHARS   = 1_800      # Hard cap on returned string (~450 tokens)
SNIPPET_MAX_CHARS  = 400        # Per-result snippet budget before compression
REQUEST_TIMEOUT    = 8          # Seconds per HTTP request
USER_AGENT         = (
    "Mozilla/5.0 (compatible; DeepResearchBot/1.0; +https://dify.ai)"
)

# ──────────────────────────────────────────────────────────────────────────────
# DATA TYPES
# ──────────────────────────────────────────────────────────────────────────────

class SearchResult(NamedTuple):
    title:   str
    url:     str
    snippet: str
    source:  str   # "google_news" | "duckduckgo"


# ──────────────────────────────────────────────────────────────────────────────
# UTILITY HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _http_get(url: str, extra_headers: dict | None = None) -> str:
    """Fetch URL and return decoded body; raises on non-200 or timeout."""
    headers = {"User-Agent": USER_AGENT}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def _strip_html(raw: str) -> str:
    """Remove all HTML tags and decode common entities."""

    class _Stripper(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts: list[str] = []

        def handle_data(self, data: str):
            self.parts.append(data)

    stripper = _Stripper()
    stripper.feed(raw)
    text = " ".join(stripper.parts)
    # Collapse whitespace
    return re.sub(r"\s+", " ", text).strip()


def _compress(text: str, budget: int) -> str:
    """
    Lightweight lossy compression:
    1. Remove filler phrases.
    2. Truncate to `budget` chars at a sentence boundary.
    """
    fillers = [
        r"\b(click here|read more|subscribe|sign up|cookie policy"
        r"|privacy policy|terms of (use|service)|all rights reserved"
        r"|advertisement|sponsored|loading\.\.\.)\b",
    ]
    for pattern in fillers:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)

    text = re.sub(r"\s+", " ", text).strip()

    if len(text) <= budget:
        return text

    # Truncate at last sentence end within budget
    truncated = text[:budget]
    last_period = max(truncated.rfind("."), truncated.rfind("!"), truncated.rfind("?"))
    if last_period > budget // 2:
        return truncated[: last_period + 1]
    return truncated.rstrip() + "…"


# ──────────────────────────────────────────────────────────────────────────────
# SOURCE 1 — GOOGLE NEWS RSS
# ──────────────────────────────────────────────────────────────────────────────

def _fetch_google_news(query: str) -> list[SearchResult]:
    """
    Pull top results from Google News RSS (no API key needed).
    Endpoint: https://news.google.com/rss/search?q=<query>&hl=en-US&gl=US&ceid=US:en
    """
    encoded = urllib.parse.quote_plus(query)
    url = (
        f"https://news.google.com/rss/search"
        f"?q={encoded}&hl=en-US&gl=US&ceid=US:en"
    )
    try:
        body = _http_get(url)
    except Exception as exc:
        print(f"[WARN] Google News RSS failed: {exc}", file=sys.stderr)
        return []

    results: list[SearchResult] = []
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        print(f"[WARN] Google News XML parse error: {exc}", file=sys.stderr)
        return []

    for item in root.iter("item"):
        if len(results) >= TOP_N_RESULTS:
            break

        title_el   = item.find("title")
        link_el    = item.find("link")
        desc_el    = item.find("description")

        title   = _strip_html(title_el.text   or "") if title_el   is not None else ""
        link    = (link_el.text or "").strip()        if link_el    is not None else ""
        snippet = _strip_html(desc_el.text    or "") if desc_el    is not None else ""

        if not title:
            continue

        results.append(SearchResult(
            title=title,
            url=link,
            snippet=_compress(snippet, SNIPPET_MAX_CHARS),
            source="google_news",
        ))

    return results


# ──────────────────────────────────────────────────────────────────────────────
# SOURCE 2 — DUCKDUCKGO HTML SCRAPE (fallback)
# ──────────────────────────────────────────────────────────────────────────────

class _DDGParser(HTMLParser):
    """
    Minimal state-machine parser for DuckDuckGo's HTML results page.
    Targets <h2> → <a> for titles/URLs and `.result__snippet` divs for snippets.
    """

    def __init__(self):
        super().__init__()
        self.results:     list[SearchResult] = []
        self._in_result   = False
        self._in_title    = False
        self._in_snippet  = False
        self._cur_title   = ""
        self._cur_url     = ""
        self._cur_snippet = ""
        self._depth       = 0   # nesting depth inside snippet div

    # helpers
    def _reset_cur(self):
        self._cur_title = self._cur_url = self._cur_snippet = ""
        self._in_result = self._in_title = self._in_snippet = False
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        classes   = attr_dict.get("class", "")

        # Detect start of a result block
        if tag == "div" and "result__body" in classes:
            self._reset_cur()
            self._in_result = True
            return

        if self._in_result:
            if tag == "h2" and "result__title" in classes:
                self._in_title = True
            elif tag == "a" and self._in_title and not self._cur_url:
                href = attr_dict.get("href", "")
                # DuckDuckGo uses /l/?uddg= redirect links
                parsed = urllib.parse.urlparse(href)
                qs     = urllib.parse.parse_qs(parsed.query)
                self._cur_url = qs.get("uddg", [href])[0]
            elif tag == "div" and "result__snippet" in classes:
                self._in_snippet = True
                self._depth = 1
            elif self._in_snippet:
                self._depth += 1

    def handle_endtag(self, tag):
        if self._in_snippet and tag == "div":
            self._depth -= 1
            if self._depth <= 0:
                self._in_snippet = False
                # Snippet closed — save result if we have enough data
                if self._cur_title and len(self.results) < TOP_N_RESULTS:
                    self.results.append(SearchResult(
                        title=self._cur_title.strip(),
                        url=self._cur_url,
                        snippet=_compress(self._cur_snippet.strip(), SNIPPET_MAX_CHARS),
                        source="duckduckgo",
                    ))
                    self._reset_cur()
        if tag == "h2":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self._cur_title += data
        elif self._in_snippet:
            self._cur_snippet += data


def _fetch_duckduckgo(query: str) -> list[SearchResult]:
    """Scrape DuckDuckGo HTML search results (no API key needed)."""
    encoded = urllib.parse.quote_plus(query)
    url     = f"https://html.duckduckgo.com/html/?q={encoded}"
    try:
        body = _http_get(url, extra_headers={"Accept-Language": "en-US,en;q=0.9"})
    except Exception as exc:
        print(f"[WARN] DuckDuckGo fetch failed: {exc}", file=sys.stderr)
        return []

    parser = _DDGParser()
    parser.feed(body)
    return parser.results


# ──────────────────────────────────────────────────────────────────────────────
# AGGREGATOR
# ──────────────────────────────────────────────────────────────────────────────

def _gather_results(query: str) -> list[SearchResult]:
    """Try Google News RSS first; fall back to DuckDuckGo."""
    results = _fetch_google_news(query)
    if len(results) < TOP_N_RESULTS:
        ddg = _fetch_duckduckgo(query)
        # Merge, deduplicate by URL
        seen = {r.url for r in results}
        for r in ddg:
            if r.url not in seen:
                results.append(r)
                seen.add(r.url)
            if len(results) >= TOP_N_RESULTS:
                break
    return results[:TOP_N_RESULTS]


# ──────────────────────────────────────────────────────────────────────────────
# OUTPUT FORMATTER — token-budget aware
# ──────────────────────────────────────────────────────────────────────────────

def _format_output(query: str, results: list[SearchResult]) -> str:
    """
    Produce a compact, plain-text summary capped at MAX_OUTPUT_CHARS.

    Format (designed for downstream LLM consumption):
    ───────────────────────────────────────────────
    QUERY: <query>
    SOURCES: <N> | <provider mix>

    [1] <Title> (<source>)
    URL: <url>
    <snippet>

    [2] ...
    ───────────────────────────────────────────────
    """
    if not results:
        return (
            f"QUERY: {query}\n"
            "SOURCES: 0\n"
            "NO_RESULTS: Could not retrieve data. "
            "Check network access or broaden the search query."
        )

    sources_label = ", ".join(sorted({r.source for r in results}))
    lines: list[str] = [
        f"QUERY: {query}",
        f"SOURCES: {len(results)} | {sources_label}",
        "",
    ]

    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r.title} ({r.source})")
        if r.url:
            lines.append(f"URL: {r.url}")
        if r.snippet:
            # Wrap snippet to keep the block readable inside Dify logs
            wrapped = textwrap.fill(r.snippet, width=100)
            lines.append(wrapped)
        lines.append("")   # blank separator

    raw = "\n".join(lines).strip()
    return _compress(raw, MAX_OUTPUT_CHARS)


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC ENTRY-POINT (Dify Tool / Code node calls this)
# ──────────────────────────────────────────────────────────────────────────────

def run(query: str) -> str:
    """
    Primary entry-point for the Dify orchestrator.

    Parameters
    ----------
    query : str
        The search query produced by the Dify planner node.

    Returns
    -------
    str
        A compressed, plain-text summary of the top search results,
        guaranteed to fit within MAX_OUTPUT_CHARS (~450 tokens).
    """
    query = query.strip()
    if not query:
        return "ERROR: Empty query received."

    results = _gather_results(query)
    return _format_output(query, results)


# ──────────────────────────────────────────────────────────────────────────────
# CLI — for local testing outside Dify
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python deep_research_tool.py \"<search query>\"")
        sys.exit(1)

    search_query = " ".join(sys.argv[1:])
    print("=" * 70)
    print(f"Deep Research Tool  |  budget: {MAX_OUTPUT_CHARS} chars")
    print("=" * 70)
    output = run(search_query)
    print(output)
    print("=" * 70)
    print(f"Output length: {len(output)} chars  (~{len(output)//4} tokens)")
