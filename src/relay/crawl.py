"""Website sync: crawl a server's docs site into its knowledge base.

The start URL comes from a server admin, so it is treated as untrusted:
https only, same host only, redirects followed manually (max 3, each
re-checked), and the host must resolve to a public IP address -- the bot
must never be usable to probe private networks or cloud metadata services.
Pages are capped in size and count, robots.txt is honored, and the crawl is
sequential so it is polite to the site.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import aiohttp
import aiohttp.abc

logger = logging.getLogger(__name__)

USER_AGENT = "RelayBot/1.0 (+Discord support bot; docs sync)"
MAX_PAGE_BYTES = 1_000_000
PAGE_TIMEOUT_SECONDS = 10.0
MAX_REDIRECTS = 3
MIN_PAGE_CHARS = 80  # skip near-empty pages (nav stubs, redirects)

_SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "header", "footer", "form", "button", "iframe"}
_BLOCK_TAGS = {
    "p", "div", "section", "article", "main", "br", "li", "ul", "ol", "tr", "table",
    "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "dd", "dt", "hr",
}  # fmt: skip
_SKIP_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".pdf", ".zip", ".gz", ".mp4", ".mp3",
    ".css", ".js", ".json", ".xml", ".woff", ".woff2", ".ttf", ".exe", ".dmg",
)  # fmt: skip


class CrawlError(RuntimeError):
    """The start URL is unusable (bad scheme, private host, unreachable)."""


@dataclass(frozen=True)
class Page:
    url: str
    title: str
    text: str


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self.title = ""
        self.h1 = ""
        self._skip_depth = 0
        self._in_title = False
        self._in_h1 = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        if tag == "title":
            self._in_title = True
        if tag == "h1":
            self._in_h1 = True
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False
        if tag == "h1":
            self._in_h1 = False
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skip_depth:
            return
        if self._in_h1:
            self.h1 += data
        self.parts.append(data)


def html_to_page(url: str, html: str) -> tuple[Page, list[str]]:
    """Extract readable text, a title, and outgoing links from HTML."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    lines = [" ".join(line.split()) for line in "".join(parser.parts).split("\n")]
    text = "\n".join(line for line in lines if line)
    # Paragraph breaks between blocks help the chunker keep sections together.
    text = text.replace("\n", "\n\n")
    title = " ".join((parser.h1 or parser.title).split()) or url
    return Page(url=url, title=title[:200], text=text), parser.links


def normalize_url(url: str, *, allow_http: bool = False) -> str | None:
    """Canonical https URL without fragment/query, or None if unusable."""
    url, _ = urldefrag(url.strip())
    parts = urlsplit(url)
    schemes = ("https", "http") if allow_http else ("https",)
    if parts.scheme not in schemes or not parts.hostname:
        return None
    path = parts.path or "/"
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, "", ""))


def in_scope(url: str, root: str) -> bool:
    """Same host, and under the start URL's directory (so syncing
    example.com/docs/ doesn't wander into the blog)."""
    u, r = urlsplit(url), urlsplit(root)
    if u.netloc != r.netloc:
        return False
    root_dir = r.path if r.path.endswith("/") else r.path.rsplit("/", 1)[0] + "/"
    return u.path.startswith(root_dir) or u.path == r.path


def is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return addr.is_global and not addr.is_multicast


Resolver = Callable[[str], Awaitable[list[str]]]


async def resolve_host(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


class PublicOnlyResolver(aiohttp.abc.AbstractResolver):
    """Connection-time guard: the crawl session can only ever connect to
    public IPs, even if DNS changes between our pre-check and the request
    (DNS rebinding)."""

    def __init__(self) -> None:
        self._inner = aiohttp.ThreadedResolver()

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> list[dict]:
        results = [r for r in await self._inner.resolve(host, port, family) if is_public_ip(r["host"])]
        if not results:
            raise OSError(f"{host} does not resolve to a public address")
        return results

    async def close(self) -> None:
        await self._inner.close()


def make_crawl_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=PublicOnlyResolver()))


class Crawler:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        resolver: Resolver = resolve_host,
        allow_private: bool = False,  # tests only: crawl a local http server
    ) -> None:
        self.session = session
        self.resolver = resolver
        self.allow_private = allow_private
        self.allow_http = allow_private
        self._host_ok: dict[str, bool] = {}

    async def _check_host(self, url: str) -> None:
        host = urlsplit(url).hostname or ""
        if host not in self._host_ok:
            try:
                ips = await self.resolver(host)
            except OSError as e:
                raise CrawlError(f"can't resolve {host}") from e
            self._host_ok[host] = bool(ips) and (self.allow_private or all(is_public_ip(ip) for ip in ips))
        if not self._host_ok[host]:
            raise CrawlError(f"{host} is not a public host")

    async def _get(self, url: str) -> tuple[str, str, str] | None:
        """GET with manually-checked redirects. Returns (final_url,
        content_type, body) or None for non-200 / non-text responses."""
        for _ in range(MAX_REDIRECTS + 1):
            await self._check_host(url)
            async with self.session.get(
                url,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=PAGE_TIMEOUT_SECONDS),
                headers={"User-Agent": USER_AGENT},
            ) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    target = normalize_url(urljoin(url, resp.headers.get("Location", "")), allow_http=self.allow_http)
                    if target is None or urlsplit(target).netloc != urlsplit(url).netloc:
                        return None
                    url = target
                    continue
                if resp.status != 200:
                    return None
                ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
                if ctype not in ("text/html", "text/plain", "text/markdown"):
                    return None
                if (resp.content_length or 0) > MAX_PAGE_BYTES:
                    return None
                # read(n) may return early with less, so accumulate chunks.
                body = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    body += chunk
                    if len(body) > MAX_PAGE_BYTES:
                        return None
                try:
                    encoding = resp.get_encoding()
                except RuntimeError:
                    encoding = "utf-8"
                return url, ctype, bytes(body).decode(encoding or "utf-8", errors="replace")
        return None

    async def _robots(self, root: str) -> RobotFileParser:
        parts = urlsplit(root)
        rp = RobotFileParser()
        try:
            got = await self._get(urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", "")))
        except (aiohttp.ClientError, TimeoutError, CrawlError, OSError):
            got = None
        rp.parse(got[2].splitlines() if got and got[1] == "text/plain" else [])
        return rp

    async def crawl(self, start_url: str, max_pages: int) -> list[Page]:
        root = normalize_url(start_url, allow_http=self.allow_http)
        if root is None:
            raise CrawlError("use a full https:// URL")
        await self._check_host(root)  # fail fast with a clear error
        robots = await self._robots(root)

        pages: list[Page] = []
        seen: set[str] = {root}
        queue: deque[str] = deque([root])
        fetched = 0
        # Fetch budget: a few more requests than pages, since some are skipped.
        while queue and len(pages) < max_pages and fetched < max_pages * 2 + 5:
            url = queue.popleft()
            if not robots.can_fetch(USER_AGENT, url):
                continue
            fetched += 1
            try:
                got = await self._get(url)
            except (aiohttp.ClientError, TimeoutError, OSError) as e:
                if url == root:
                    raise CrawlError(f"couldn't fetch {root}: {e}") from e
                logger.info("crawl: skipping %s (%s)", url, e)
                continue
            if got is None:
                if url == root:
                    raise CrawlError(f"{root} didn't return a readable page")
                continue
            final_url, ctype, body = got
            if ctype == "text/html":
                page, links = html_to_page(final_url, body)
            else:
                page, links = Page(url=final_url, title=final_url.rsplit("/", 1)[-1] or final_url, text=body), []
            if len(page.text) >= MIN_PAGE_CHARS:
                pages.append(page)
            for href in links:
                link = normalize_url(urljoin(final_url, href), allow_http=self.allow_http)
                if (
                    link
                    and link not in seen
                    and in_scope(link, root)
                    and not urlsplit(link).path.lower().endswith(_SKIP_EXTENSIONS)
                ):
                    seen.add(link)
                    queue.append(link)
        return pages
