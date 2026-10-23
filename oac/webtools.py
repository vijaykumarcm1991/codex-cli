"""Read-only web fetching for oac: HTTP GET plus a stdlib HTML-to-text converter."""
from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

MAX_BYTES = 2_000_000
DEFAULT_MAX_CHARS = 24_000
USER_AGENT = "oac-terminal-agent (+https://github.com/vijaykumarcm1991/codex-cli)"

BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "body", "br", "canvas", "dd", "div",
    "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
    "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
    "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
}
SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe", "canvas"}


class TextExtractor(HTMLParser):
    """Convert HTML to readable plain text: drops scripts/styles, keeps block layout."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._skip_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "title":
            self._in_title = True
        elif tag in SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "li":
            self._chunks.append("\n- ")
        elif tag == "br":
            self._chunks.append("\n")
        elif tag in BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in BLOCK_TAGS or tag == "li":
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
        else:
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        # Collapse runs of whitespace but keep line structure, then squash blank runs.
        lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in raw.split("\n")]
        cleaned: list[str] = []
        blank = False
        for line in lines:
            if line or line == "- ":  # keep list bullets that carry content later
                if line == "- ":
                    line = "-"
                if line:
                    cleaned.append(line)
                    blank = False
                    continue
            if cleaned and not blank:
                cleaned.append("")
                blank = True
        while cleaned and not cleaned[-1]:
            cleaned.pop()
        return "\n".join(cleaned)


def html_to_text(html: str) -> tuple[str, str]:
    """Return ``(title, body_text)`` for an HTML document."""
    extractor = TextExtractor()
    try:
        extractor.feed(html)
        extractor.close()
    except Exception:  # malformed HTML still yields whatever parsed so far
        pass
    return extractor.title.strip(), extractor.text()


def fetch(url: str, max_chars: int = DEFAULT_MAX_CHARS, timeout: float = 20.0) -> str:
    """GET a document and return title + readable text, truncated to max_chars."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("URL must be http(s) with a host")
    if parsed.username or parsed.password:
        raise ValueError("URLs with embedded credentials are not allowed")
    max_chars = min(60_000, max(200, int(max_chars)))
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain,*/*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if status >= 400:
                raise ValueError(f"HTTP {status} fetching {url}")
            content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            raw = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"HTTP {exc.code} fetching {url}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ValueError(f"Fetch failed: {exc}") from None
    if len(raw) > MAX_BYTES:
        raw = raw[:MAX_BYTES]
        truncated_body = True
    else:
        truncated_body = False
    charset = "utf-8"
    match = re.search(r"charset=([\w-]+)", (response.headers.get("Content-Type") or ""), re.I)
    if match:
        charset = match.group(1)
    try:
        body = raw.decode(charset, errors="replace")
    except LookupError:
        body = raw.decode("utf-8", errors="replace")
    if "html" in content_type or (not content_type and body.lstrip()[:1] == "<"):
        title, text = html_to_text(body)
    elif content_type in {"text/plain", "text/markdown", "application/json", ""}:
        title, text = "", body
    else:
        raise ValueError(f"Unsupported content type {content_type!r}; only text and HTML can be fetched")
    header = f"# {title}\n" if title else ""
    if not text.strip():
        raise ValueError("The document contained no readable text")
    clipped = text[:max_chars]
    note = "\n[document truncated]" if len(text) > max_chars or truncated_body else ""
    return f"{header}{clipped}{note}"
