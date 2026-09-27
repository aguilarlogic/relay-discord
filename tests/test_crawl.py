import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from relay.crawl import (
    MAX_PAGE_BYTES,
    Crawler,
    CrawlError,
    html_to_page,
    in_scope,
    is_public_ip,
    normalize_url,
)

LONG = "Useful documentation text. " * 10


def page(title, body, links=()):
    anchors = "".join(f'<a href="{href}">link</a>' for href in links)
    return (
        f"<html><head><title>{title}</title><script>var secret = 1;</script></head>"
        f"<body><nav>Home | Pricing</nav><h1>{title}</h1><p>{body}</p>{anchors}<footer>(c) 2026</footer></body></html>"
    )


@pytest.fixture
async def site():
    routes = {
        "/docs/": page(
            "Docs home",
            LONG,
            [
                "/docs/install",
                "install",
                "/docs/refunds#top",
                "/blog/news",
                "https://elsewhere.example/x",
                "/docs/logo.png",
                "/docs/private",
                "/docs/moved",
                "/docs/big",
                "mailto:a@b.c",
            ],
        ),
        "/docs/install": page("Install", "Run the installer. " + LONG, ["/docs/"]),
        "/docs/refunds": page("Refunds", "Refunds within 14 days. " + LONG),
        "/docs/private": page("Private", "secret " + LONG),
        "/docs/big": page("Big", "x" * (MAX_PAGE_BYTES + 10)),
        "/blog/news": page("News", LONG),
    }
    fetched: list[str] = []

    async def handler(request: web.Request) -> web.Response:
        fetched.append(request.path)
        if request.path == "/robots.txt":
            return web.Response(text="User-agent: *\nDisallow: /docs/private\n", content_type="text/plain")
        if request.path == "/docs/moved":
            raise web.HTTPFound("https://evil.example/steal")
        if request.path in routes:
            return web.Response(text=routes[request.path], content_type="text/html")
        return web.Response(status=404)

    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    server = TestServer(app)
    await server.start_server()
    yield str(server.make_url("/docs/")), fetched
    await server.close()


async def test_crawl_scope_robots_and_limits(site):
    start, fetched = site
    async with ClientSession() as session:
        pages = await Crawler(session, allow_private=True).crawl(start, max_pages=10)
    titles = sorted(p.title for p in pages)
    assert titles == ["Docs home", "Install", "Refunds"]
    assert "/docs/private" not in fetched  # robots.txt
    assert "/blog/news" not in fetched  # outside the start directory
    assert "/docs/logo.png" not in fetched
    home = next(p for p in pages if p.title == "Docs home")
    assert "secret" not in home.text and "Pricing" not in home.text and "(c) 2026" not in home.text


async def test_crawl_respects_page_budget(site):
    start, _ = site
    async with ClientSession() as session:
        pages = await Crawler(session, allow_private=True).crawl(start, max_pages=2)
    assert len(pages) == 2


async def test_private_hosts_are_refused():
    async def resolver(host):
        return ["10.0.0.5"]

    async with ClientSession() as session:
        with pytest.raises(CrawlError, match="not a public host"):
            await Crawler(session, resolver=resolver).crawl("https://docs.example.com/", max_pages=5)


async def test_http_urls_are_refused_in_production():
    async with ClientSession() as session:
        with pytest.raises(CrawlError, match="https"):
            await Crawler(session).crawl("http://docs.example.com/", max_pages=5)


def test_normalize_and_scope():
    assert normalize_url("https://Docs.Example.com/a/b?x=1#frag") == "https://docs.example.com/a/b"
    assert normalize_url("https://example.com") == "https://example.com/"
    assert normalize_url("http://example.com/") is None
    assert normalize_url("http://example.com/", allow_http=True) == "http://example.com/"
    assert normalize_url("javascript:alert(1)") is None
    assert in_scope("https://x.dev/docs/a", "https://x.dev/docs/")
    assert in_scope("https://x.dev/docs/a", "https://x.dev/docs/index.html")
    assert not in_scope("https://x.dev/blog", "https://x.dev/docs/")
    assert not in_scope("https://y.dev/docs/a", "https://x.dev/docs/")


@pytest.mark.parametrize(
    ("ip", "public"),
    [
        ("8.8.8.8", True),
        ("10.1.2.3", False),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("192.168.1.1", False),
        ("::1", False),
        ("fd00::1", False),
        ("2606:4700::1111", True),
    ],
)
def test_is_public_ip(ip, public):
    assert is_public_ip(ip) is public


def test_html_to_page_title_and_links():
    p, links = html_to_page("https://x.dev/a", page("Hello", "Body text", ["/b"]))
    assert p.title == "Hello" and "Body text" in p.text and links == ["/b"]
