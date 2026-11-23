"""
Reading one web page you shared from the phone (Share -> ARYA) — on the laptop, no cloud AI involved.

fetch(url) downloads just that page (or PDF) and returns its title and readable text, which then goes through the
same path as an uploaded document (arya/core/share.py -> attachments.accept): indexed, treated as untrusted.

Guard rails: http/https only; never a private, loopback, link-local or Tailscale address (a shared link must not
make ARYA poke the router, the phone or its own servers) — checked for every redirect too; at most a few MB.
No new dependency: the article text is pulled out with the standard library's HTML parser.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urlsplit

MAX_BYTES = 5 * 1024 * 1024
MAX_CHARS = 60_000
TIMEOUT_S = 15.0
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
_TAILNET = ipaddress.ip_network("100.64.0.0/10")


class PageError(Exception):
    """Something to say to the user ("That page couldn't be reached.")."""


def _blocked_ip(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_reserved or a.is_unspecified
            or (isinstance(a, ipaddress.IPv4Address) and a in _TAILNET))


def check_url(url: str) -> str:
    """The URL if ARYA may fetch it, else PageError. Resolves the host (blocking: call in a thread)."""
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise PageError("I can only open web links (http or https).")
    host = parts.hostname
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme.lower() == "https" else 80),
                                   type=socket.SOCK_STREAM)
    except OSError:
        raise PageError(f"I couldn't find {host} — is the laptop online?") from None
    if not infos or any(_blocked_ip(info[4][0]) for info in infos):
        raise PageError("That link points into a private network, so I won't open it.")
    return url.strip()


# ── Pulling the readable text out of HTML ────────────────────────────────────

_SKIP = {"script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "form", "button", "iframe",
         "template", "select", "figure"}
_BLOCK = {"p", "div", "section", "article", "main", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
          "tr", "table", "blockquote", "pre", "dd", "dt", "hr"}
_VOID = {"br", "hr", "img", "input", "meta", "link", "source", "wbr", "area", "base", "col", "embed", "track"}


# Page furniture marked by ARIA role or a class / id word ("sphinxsidebar", "site-nav", "cookie-banner" …)
_SKIP_ROLES = {"navigation", "banner", "contentinfo", "complementary", "search", "menu", "menubar", "dialog"}
_SKIP_WORDS = {"nav", "navbar", "navigation", "sidebar", "sphinxsidebar", "related", "menu", "footer", "breadcrumb",
               "breadcrumbs", "cookie", "cookies", "consent", "subscribe", "newsletter", "share", "social", "advert",
               "ads", "promo", "related-posts", "comments", "toc"}


def _is_furniture(attrs, by_name: bool = True) -> bool:
    a = {k: (v or "") for k, v in attrs}
    if a.get("role", "").lower() in _SKIP_ROLES or "hidden" in a or a.get("aria-hidden") == "true":
        return True
    if not by_name:
        return False
    words = set(re.split(r"[\s_-]+", (a.get("class", "") + " " + a.get("id", "")).lower()))
    return bool(words & _SKIP_WORDS)


class _Extractor(HTMLParser):
    def __init__(self, by_name: bool = True) -> None:
        super().__init__(convert_charrefs=True)
        self.by_name = by_name              # also skip by class / id words (off for the second try)
        self.title = ""
        self._in_title = False
        self._skip = 0
        self._stack: list[tuple[str, bool, bool]] = []      # (tag, skipped, main content)
        self.all: list[str] = []
        self.main: list[str] = []           # text inside <article> / <main> / role="main"
        self._main_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _VOID:
            if tag in _BLOCK:
                self._emit("\n")
            return
        skip = tag in _SKIP or _is_furniture(attrs, self.by_name)
        main = tag in ("article", "main") or dict(attrs).get("role") == "main"
        self._stack.append((tag, skip, main))
        if tag == "title":
            self._in_title = True
        if skip:
            self._skip += 1
        if main:
            self._main_depth += 1
        if tag in _BLOCK:
            self._emit("\n")
        if tag in ("h1", "h2", "h3"):
            self._emit("## " if tag != "h1" else "# ")
        if tag == "li":
            self._emit("- ")

    def handle_endtag(self, tag: str) -> None:
        if not any(t == tag for t, _, _ in self._stack):
            return
        while self._stack:                      # close whatever wasn't closed properly
            top, skip, main = self._stack.pop()
            if top == "title":
                self._in_title = False
            if skip:
                self._skip = max(0, self._skip - 1)
            if main:
                self._main_depth = max(0, self._main_depth - 1)
            if top in _BLOCK:
                self._emit("\n")
            if top == tag:
                break

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skip:
            return
        self._emit(data)

    def _emit(self, s: str) -> None:
        self.all.append(s)
        if self._main_depth:
            self.main.append(s)


def _tidy(parts: list[str]) -> str:
    text = "".join(parts)
    text = re.sub(r"[ \t\r\f\v ]+", " ", text)
    lines = [ln.strip() for ln in text.split("\n")]
    out, blank = [], False
    for ln in lines:
        if not ln or ln in ("-", "#", "##"):
            if out and not blank:
                out.append("")
            blank = True
            continue
        out.append(ln)
        blank = False
    return "\n".join(out).strip()


def extract(html: str) -> tuple[str, str]:
    """(title, readable text) of an HTML page: the <article>/<main> part when there is a real one, else the body
    without navigation, headers, footers, scripts and forms. Skipping by class names ("sidebar", "footer" …) can
    hit a wrapper around the whole page (arXiv: "flex-wrap-footer"): then a second try without that rule."""
    best = ("", "")
    for by_name in (True, False):
        p = _Extractor(by_name)
        try:
            p.feed(html)
            p.close()
        except Exception:
            pass
        title = re.sub(r"\s+", " ", p.title).strip()
        main, body = _tidy(p.main), _tidy(p.all)
        text = main if len(main) >= 400 or (main and len(main) >= 0.4 * len(body)) else body
        if len(text) > len(best[1]):
            best = (title, text)
        if len(text) >= 300:
            break
    return best[0], best[1][:MAX_CHARS]


# ── Fetching ─────────────────────────────────────────────────────────────────

def _meta_charset(head: bytes) -> str | None:
    """<meta charset="…"> (or the http-equiv form) near the top of the page, when the server didn't say."""
    m = re.search(rb"""<meta[^>]+charset\s*=\s*["']?\s*([\w.:-]+)""", head, re.IGNORECASE)
    return m.group(1).decode("ascii", "ignore") if m else None


class Page:
    def __init__(self, url: str, title: str, text: str = "", pdf: bytes = b"") -> None:
        self.url, self.title, self.text, self.pdf = url, title, text, pdf


async def fetch(url: str) -> Page:
    """Downloads one page (HTML, plain text or PDF). PageError with a sentence to say when it can't."""
    import httpx

    await asyncio.to_thread(check_url, url)

    async def guard(request: httpx.Request) -> None:          # every redirect is checked as well
        await asyncio.to_thread(check_url, str(request.url))

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=True, max_redirects=5,
                                     headers={"User-Agent": _UA, "Accept-Language": "en,de;q=0.8"},
                                     event_hooks={"request": [guard]}) as client:
            async with client.stream("GET", url) as r:
                if r.status_code >= 400:
                    raise PageError(f"The site answered with an error ({r.status_code}).")
                kind = r.headers.get("content-type", "").split(";")[0].strip().lower()
                if kind and not (kind.startswith("text/") or kind in ("application/xhtml+xml", "application/pdf")):
                    raise PageError("That link isn't a page I can read (it's a " + kind.split("/")[0] + " file).")
                data = bytearray()
                async for chunk in r.aiter_bytes():
                    data += chunk
                    if len(data) > MAX_BYTES:
                        raise PageError("That page is too large for me to read.")
                final = str(r.url)
                encoding = r.charset_encoding or _meta_charset(bytes(data[:4096])) or "utf-8"
    except PageError:
        raise
    except httpx.HTTPError as exc:
        cause = exc.__cause__ or exc.__context__
        if isinstance(cause, PageError):
            raise cause from None
        raise PageError("That page couldn't be reached.") from None

    host = urlsplit(final).hostname or "the page"
    if kind == "application/pdf" or bytes(data[:5]) == b"%PDF-":
        name = urlsplit(final).path.rstrip("/").rsplit("/", 1)[-1] or host
        return Page(final, re.sub(r"\.pdf$", "", name, flags=re.IGNORECASE), pdf=bytes(data))
    try:
        raw = bytes(data).decode(encoding, errors="replace")
    except LookupError:                                    # a charset Python doesn't know
        raw = bytes(data).decode("utf-8", errors="replace")
    if kind == "text/plain":
        title, text = host, raw[:MAX_CHARS]
    else:
        title, text = extract(raw)
    if len(text.strip()) < 40:
        raise PageError("I couldn't find readable text on that page (it may need a login or JavaScript).")
    return Page(final, title or host, text)
