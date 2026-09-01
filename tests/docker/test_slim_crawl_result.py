"""
A finished crawl has to be small enough to hand back.

Regression test for 2026-09-01. A 500-page crawl of feelporto.com read every
page without a single failure — and delivered nothing. The result was 243 MB,
and Redis timed out returning it: the job record ended `completed` carrying the
error "Timeout writing to socket". The API asked for the pages, got an error
back, and marked the knowledge source failed.

Almost none of that 243 MB was wanted. Measured on one real page, as a share of
what was stored for it:

    html            72.3%
    fit_html        13.4%
    cleaned_html     5.6%
    links            2.5%
    markdown         5.7%   ← the only large field any caller reads

The API takes a page's markdown, its URL and its status code. Everything else
was carried across the network and thrown away.

Runs without crawl4ai or FastAPI installed: the two helpers are read straight out
of deploy/docker/utils.py with its imports stubbed.

    python3 -m unittest tests.docker.test_slim_crawl_result -v
"""
import os
import sys
import types
import unittest

DOCKER_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "deploy", "docker"
)


def _install_stubs():
    for name, attrs in (
        ("dns", {}),
        ("dns.resolver", {}),
        ("yaml", {"safe_load": lambda *a, **k: {}}),
        ("fastapi", {"Request": object}),
    ):
        if name not in sys.modules:
            module = types.ModuleType(name)
            for attr, value in attrs.items():
                setattr(module, attr, value)
            sys.modules[name] = module
    sys.modules["dns"].resolver = sys.modules["dns.resolver"]


_install_stubs()


def _load_utils():
    """
    Compile deploy/docker/utils.py under a private name.

    NOT `import utils`. The pool suites install a stub module called `utils` into
    sys.modules, and whichever test module runs first wins — running this file
    after them found the stub and failed with "module 'utils' has no attribute
    'slim_crawl_result'". Reading the source directly is immune to that, and to
    the bytecode cache macOS keeps outside the repo.
    """
    path = os.path.join(DOCKER_DIR, "utils.py")
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    module = types.ModuleType("docker_utils_under_test")
    module.__file__ = path
    exec(compile(source, path, "exec"), module.__dict__)
    return module


utils = _load_utils()

slim = utils.slim_crawl_result
dropped = utils.dropped_result_fields

OURS = ("html", "fit_html", "cleaned_html", "links")


def page(**overrides):
    base = {
        "url": "https://www.feelporto.com/en/apartments/",
        "success": True,
        "status_code": 200,
        "markdown": {"raw_markdown": "# Apartments"},
        "html": "<html>" + "x" * 5000 + "</html>",
        "fit_html": "y" * 900,
        "cleaned_html": "z" * 400,
        "links": {"internal": [1, 2, 3], "external": []},
    }
    base.update(overrides)
    return base


def result(pages=1):
    return {"success": True, "results": [page() for _ in range(pages)], "server_processing_time_s": 1.0}


class SlimmingTests(unittest.TestCase):
    def test_the_fields_nobody_reads_are_gone(self):
        out = slim(result(), OURS)
        for field in OURS:
            self.assertNotIn(field, out["results"][0], field)

    def test_everything_the_api_reads_survives(self):
        # Exactly the fields processCrawlResult touches. Losing any one of them
        # silently turns a good crawl into an empty one.
        out = slim(result(), OURS)["results"][0]
        for field in ("url", "success", "status_code", "markdown"):
            self.assertIn(field, out, field)
        self.assertEqual(out["markdown"]["raw_markdown"], "# Apartments")

    def test_it_is_very_much_smaller(self):
        import json

        before = len(json.dumps(result(50)))
        after = len(json.dumps(slim(result(50), OURS)))
        self.assertLess(after, before * 0.15, "the size problem is not actually solved")

    def test_the_envelope_around_the_pages_is_kept(self):
        out = slim(result(), OURS)
        self.assertTrue(out["success"])
        self.assertEqual(out["server_processing_time_s"], 1.0)

    def test_the_caller_s_own_copy_is_untouched(self):
        # The same object is answered to a synchronous /crawl, which promises
        # every field. Only what gets stored may be trimmed.
        original = result()
        slim(original, OURS)
        self.assertIn("html", original["results"][0])

    def test_dropping_nothing_returns_the_result_as_it_stands(self):
        original = result()
        self.assertIs(slim(original, ()), original)

    def test_a_page_that_is_not_a_dict_is_passed_through(self):
        odd = {"results": ["not a page", None]}
        self.assertEqual(slim(odd, OURS)["results"], ["not a page", None])

    def test_a_result_with_no_pages_is_left_alone(self):
        for shape in ({"success": False, "error": "nope"}, {"results": None}, {}):
            self.assertIs(slim(shape, OURS), shape)

    def test_a_field_that_is_not_there_is_not_a_problem(self):
        thin = {"results": [{"url": "https://example.com", "markdown": "hi"}]}
        self.assertEqual(slim(thin, OURS)["results"][0], {"url": "https://example.com", "markdown": "hi"})


class ConfigurationTests(unittest.TestCase):
    """Nothing is withheld unless a deployment asks for it."""

    def test_a_deployment_that_says_nothing_keeps_every_field(self):
        # This fork serves more than one thing. Silently withholding page HTML
        # from a caller that wanted it is a worse bug than the size it saves.
        for config in ({}, {"crawler": {}}, {"crawler": {"jobs": {}}}, {"crawler": {"jobs": None}}, None):
            self.assertEqual(dropped(config), (), repr(config))

    def test_the_configured_fields_are_read(self):
        config = {"crawler": {"jobs": {"drop_result_fields": ["html", "links"]}}}
        self.assertEqual(dropped(config), ("html", "links"))

    def test_an_empty_list_means_keep_everything(self):
        self.assertEqual(dropped({"crawler": {"jobs": {"drop_result_fields": []}}}), ())

    def test_our_own_config_drops_the_bulk_and_keeps_the_markdown(self):
        # Read from the file that actually ships, so an edit there that dropped
        # `markdown` — or stopped dropping `html` — fails here rather than in a
        # customer's knowledge base.
        import re

        path = os.path.join(DOCKER_DIR, "config.yml")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        block = re.search(r"drop_result_fields:\n((?:\s*-\s*\w+\n)+)", text)
        self.assertIsNotNone(block, "config.yml no longer configures this")
        fields = set(re.findall(r"-\s*(\w+)", block.group(1)))
        self.assertIn("html", fields, "the biggest field is being stored again")
        for needed in ("markdown", "url", "status_code", "success"):
            self.assertNotIn(needed, fields, f"{needed} is read by the API and must not be dropped")


if __name__ == "__main__":
    unittest.main()
