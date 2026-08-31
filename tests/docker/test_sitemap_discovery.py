"""
A redirect is not proof that a sitemap exists.

Regression test for 2026-08-31. Sitemap discovery probes `/sitemap.xml` then
`/sitemap_index.xml`, on https and then http, and takes whatever `_resolve_head`
returns as the sitemap's location. `_resolve_head` returns the redirect target for
any 3xx — and `http://` → `https://` is a site-wide rule that answers for every
URL, real or not. feelporto.com:

    HEAD https://www.feelporto.com/sitemap.xml        -> 404
    HEAD https://www.feelporto.com/sitemap_index.xml  -> 404
    HEAD http://www.feelporto.com/sitemap.xml         -> 301  https://…/sitemap.xml
    GET  https://www.feelporto.com/sitemap.xml        -> 404

So `sitemap_url` was set to a page that 404s. There IS a robots.txt fallback, and
it would have worked — the site lists its real sitemap at /sitemaps.xml — but that
branch only runs when no sitemap URL was found at all. A truthy `sitemap_url`
skipped it.

The cost: the site publishes 274 pages including every individual apartment, and we
crawled none of them. Link-following instead spent the page budget on filter and
pagination pages. `POST /seed` for that site returned **0 urls** in production.

Loaded under its own package name with httpx and aiofiles stubbed, so it exercises
the real `_from_sitemaps` without crawl4ai installed — and without disturbing the
`crawl4ai` stub the pool tests install.

    python3 -m unittest tests.docker.test_sitemap_discovery -v
"""
import asyncio
import importlib.util
import os
import sys
import types
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PKG = "seeder_under_test"


def _install_stubs():
    if "aiofiles" not in sys.modules:
        sys.modules["aiofiles"] = types.ModuleType("aiofiles")

    if "httpx" not in sys.modules:
        httpx = types.ModuleType("httpx")

        class HTTPStatusError(Exception):
            def __init__(self, *a, response=None, **kw):
                super().__init__(*a)
                self.response = response

        class RequestError(Exception):
            pass

        httpx.HTTPStatusError = HTTPStatusError
        httpx.RequestError = RequestError
        httpx.AsyncClient = object
        sys.modules["httpx"] = httpx

    if PKG not in sys.modules:
        pkg = types.ModuleType(PKG)
        pkg.__path__ = [os.path.join(REPO_ROOT, "crawl4ai")]
        sys.modules[PKG] = pkg

        logger_mod = types.ModuleType(f"{PKG}.async_logger")

        class AsyncLoggerBase:
            pass

        class AsyncLogger(AsyncLoggerBase):
            pass

        logger_mod.AsyncLoggerBase = AsyncLoggerBase
        logger_mod.AsyncLogger = AsyncLogger
        sys.modules[f"{PKG}.async_logger"] = logger_mod


_install_stubs()


def _load_seeder():
    name = f"{PKG}.async_url_seeder"
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(REPO_ROOT, "crawl4ai", "async_url_seeder.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


seeder_mod = _load_seeder()


ROBOTS = """User-agent: *

Sitemap: https://www.feelporto.com/sitemaps.xml

Disallow: /pt2020/
Noindex : /intranet/signin/login
"""

SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
    <sitemap><loc>https://www.feelporto.com/web-sitemap/sitemap.xml</loc></sitemap>
</sitemapindex>"""

PAGES = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
    <url><loc>https://www.feelporto.com/aluguer/apartamento-porto-clerigos-charm-1-2-502650.html</loc></url>
    <url><loc>https://www.feelporto.com/aluguer/estudio-porto-galerias-cotton-cozy-227094.html</loc></url>
    <url><loc>https://www.feelporto.com/aluguer/apartamento-matosinhos-ocean-flat-iii-386329.html</loc></url>
    <url><loc>https://www.feelporto.com/sobre-nos/</loc></url>
    <url><loc>https://www.feelporto.com/experiencias/</loc></url>
</urlset>"""


class Response:
    def __init__(self, status_code, text="", headers=None, url=None):
        self.status_code = status_code
        self.text = text
        self.content = text.encode()
        self.headers = headers or {}
        self.url = url

    def raise_for_status(self):
        if self.status_code >= 400:
            raise sys.modules["httpx"].HTTPStatusError("bad status", response=self)


class FeelPortoClient:
    """The site as it actually answered, including the http→https redirect."""

    def __init__(self):
        self.head_calls = []
        self.get_calls = []

    async def head(self, url, timeout=None, follow_redirects=False):
        self.head_calls.append(url)
        if url.startswith("http://"):
            # Site-wide scheme redirect. Answers for every URL, real or not.
            return Response(301, headers={"location": url.replace("http://", "https://", 1)}, url=url)
        return Response(404, url=url)

    async def get(self, url, timeout=None, follow_redirects=False):
        self.get_calls.append(url)
        if url.endswith("/robots.txt"):
            return Response(200, ROBOTS, url=url)
        if url.endswith("/sitemaps.xml"):
            return Response(200, SITEMAP_INDEX, url=url)
        if url.endswith("/web-sitemap/sitemap.xml"):
            return Response(200, PAGES, url=url)
        # /sitemap.xml and /sitemap_index.xml do not exist on this site.
        return Response(404, "<html>not found</html>", url=url)


def collect(client, tmp_dir):
    seeder = seeder_mod.AsyncUrlSeeder(client=client)
    seeder.cache_dir = tmp_dir

    async def run():
        found = []
        async for url in seeder._from_sitemaps("www.feelporto.com", "*", force=True):
            found.append(url)
        return found

    return asyncio.run(run())


class SitemapDiscoveryTests(unittest.TestCase):
    def setUp(self):
        import pathlib
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_sitemap_is_found_through_robots_txt(self):
        client = FeelPortoClient()
        found = collect(client, self.tmp_dir)

        self.assertGreater(len(found), 0, "a redirect must not stop us reading robots.txt")
        self.assertIn(
            "https://www.feelporto.com/aluguer/apartamento-porto-clerigos-charm-1-2-502650.html",
            found,
            "the apartments are the whole point of crawling this site",
        )

    def test_robots_txt_is_actually_consulted(self):
        client = FeelPortoClient()
        collect(client, self.tmp_dir)
        self.assertIn("https://www.feelporto.com/robots.txt", client.get_calls)

    def test_a_nested_sitemap_index_is_followed(self):
        client = FeelPortoClient()
        collect(client, self.tmp_dir)
        self.assertIn("https://www.feelporto.com/web-sitemap/sitemap.xml", client.get_calls)

    def test_a_real_sitemap_at_the_usual_path_is_still_used(self):
        """The other half: a site that does publish /sitemap.xml must not regress."""

        class NormalSite(FeelPortoClient):
            async def head(self, url, timeout=None, follow_redirects=False):
                self.head_calls.append(url)
                if url == "https://www.feelporto.com/sitemap.xml":
                    return Response(200, url=url)
                return Response(404, url=url)

            async def get(self, url, timeout=None, follow_redirects=False):
                self.get_calls.append(url)
                if url.endswith("/sitemap.xml"):
                    return Response(200, PAGES, url=url)
                return Response(404, url=url)

        client = NormalSite()
        found = collect(client, self.tmp_dir)
        self.assertEqual(len(found), 5)
        self.assertNotIn(
            "https://www.feelporto.com/robots.txt",
            client.get_calls,
            "no need to consult robots.txt when the sitemap is where it should be",
        )


if __name__ == "__main__":
    unittest.main()
