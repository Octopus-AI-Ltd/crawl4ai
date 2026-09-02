"""
The message saying a crawl finished must be small enough to deliver.

Regression test for 2026-09-02. A 1,241-page crawl of feelporto.com read every
page without a single failure and produced a perfectly good 100.8 MB result. Then
the notification announcing it was rejected:

    Webhook rejected with status 413:
    {"message":"request entity too large","length":121582,"limit":102400}

The notification carried the full list of crawled URLs — 1,241 of them, about
98 bytes each. So the message announcing the crawl was 121 KB against a receiver
that accepts 100 KB, purely because of the addresses. The results were never
requested, and 1,241 read pages went uncollected while the source sat on
"in progress".

Nothing reads that list. The receiver takes `task_id`, `status` and `error`, then
goes and fetches the pages itself.

    python3 -m unittest tests.docker.test_webhook_payload_size -v
"""
import json
import os
import sys
import types
import unittest

DOCKER_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "deploy", "docker"
)

RECEIVER_LIMIT_BYTES = 102_400  # what the API's body parser accepts
OBSERVED_PAYLOAD_BYTES = 121_582  # what it was sent


def _install_stubs():
    for name in ("httpx",):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)


_install_stubs()


def _load_webhook():
    """Compiled from source, so a stubbed sibling module cannot shadow it."""
    path = os.path.join(DOCKER_DIR, "webhook.py")
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    module = types.ModuleType("webhook_under_test")
    module.__file__ = path
    exec(compile(source, path, "exec"), module.__dict__)
    return module


wh = _load_webhook()

# Shaped like the real ones — feelporto.com's apartment URLs carry a long slug
# and an id, and averaged about 98 bytes each once JSON-quoted.
FEELPORTO_URLS = [
    f"https://www.feelporto.com/en/apartments/porto-downtown-essence-flats-studio-{i}-c14u{60000 + i}.html"
    for i in range(1241)
]


def payload_for(urls, limit=None):
    """The notification as notify_job_completion builds it."""
    return {
        "task_id": "crawl_fa469129",
        "task_type": "crawl",
        "status": "completed",
        "timestamp": "2026-09-02T09:33:35.000000+00:00",
        "urls": wh.sample_urls(urls, limit),
        "url_count": len(urls),
    }


class PayloadSizeTests(unittest.TestCase):
    def test_the_crawl_that_could_not_be_announced_now_fits(self):
        size = len(json.dumps(payload_for(FEELPORTO_URLS)))
        self.assertLess(size, RECEIVER_LIMIT_BYTES, "still too big to deliver")
        # Not marginally: a notification should not be in the same order of
        # magnitude as the limit, or the next site simply moves the line.
        self.assertLess(size, RECEIVER_LIMIT_BYTES / 10)

    def test_the_untrimmed_payload_really_was_over_the_limit(self):
        # Proves the test data reproduces the bug rather than describing it.
        untrimmed = len(json.dumps({**payload_for(FEELPORTO_URLS), "urls": FEELPORTO_URLS}))
        self.assertGreater(untrimmed, RECEIVER_LIMIT_BYTES)
        self.assertGreater(untrimmed, OBSERVED_PAYLOAD_BYTES * 0.8, "not the size of the real thing")

    def test_the_real_number_is_still_reported(self):
        # Trimming the list must not quietly turn 1,241 pages into 10.
        self.assertEqual(payload_for(FEELPORTO_URLS)["url_count"], 1241)

    def test_everything_the_receiver_reads_is_untouched(self):
        payload = payload_for(FEELPORTO_URLS)
        self.assertEqual(payload["task_id"], "crawl_fa469129")
        self.assertEqual(payload["status"], "completed")


class SampleUrlsTests(unittest.TestCase):
    def test_a_normal_crawl_is_unchanged(self):
        # Most crawls are small. Their notification should look exactly as it
        # always did, so nothing reading it has to learn a new shape.
        few = ["https://example.com/a", "https://example.com/b"]
        self.assertEqual(wh.sample_urls(few), few)

    def test_a_long_list_is_cut_to_the_cap(self):
        self.assertEqual(len(wh.sample_urls(FEELPORTO_URLS)), wh.DEFAULT_MAX_URLS_IN_PAYLOAD)

    def test_it_keeps_the_first_ones_rather_than_any_ten(self):
        # The point of keeping any is being able to recognise the crawl.
        self.assertEqual(wh.sample_urls(FEELPORTO_URLS)[0], FEELPORTO_URLS[0])

    def test_a_deployment_can_choose_its_own_cap(self):
        self.assertEqual(len(wh.sample_urls(FEELPORTO_URLS, 3)), 3)
        self.assertEqual(wh.sample_urls(FEELPORTO_URLS, 0), [])

    def test_nonsense_falls_back_to_the_default(self):
        for bad in ("lots", None, -5, object()):
            self.assertEqual(len(wh.sample_urls(FEELPORTO_URLS, bad)), wh.DEFAULT_MAX_URLS_IN_PAYLOAD, repr(bad))

    def test_something_that_is_not_a_list_is_passed_through(self):
        self.assertEqual(wh.sample_urls("https://example.com"), "https://example.com")
        self.assertIsNone(wh.sample_urls(None))


class ConfiguredCapTests(unittest.TestCase):
    def test_our_own_config_sets_a_cap(self):
        import re

        with open(os.path.join(DOCKER_DIR, "config.yml"), encoding="utf-8") as handle:
            text = handle.read()
        found = re.search(r"^\s*max_urls_in_payload:\s*(\d+)", text, re.M)
        self.assertIsNotNone(found, "config.yml no longer caps the notification")
        self.assertLessEqual(int(found.group(1)), 100, "a cap this high does not solve the size problem")


if __name__ == "__main__":
    unittest.main()
