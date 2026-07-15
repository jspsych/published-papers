"""Unit tests for update_papers.py HTTP retry and Crossref cache behavior.

Run with:  python3 -m unittest discover tests

Network access is fully mocked; no HTTP requests are made.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "analysis"))

import update_papers as up  # noqa: E402
import generate_summaries as gs  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, headers=None, json_data=None,
                 json_error=False):
        self.status_code = status_code
        self.headers = headers or {}
        self._json_data = json_data
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._json_data

    def raise_for_status(self):
        raise AssertionError("raise_for_status should not be reached here")


class HttpGetJsonTests(unittest.TestCase):
    def test_malformed_200_retried_then_succeeds(self):
        """A 200 with a non-JSON body is retried; a later good 200 wins."""
        responses = [
            FakeResponse(200, json_error=True),
            FakeResponse(200, json_data={"ok": True}),
        ]
        sleeps = []
        with mock.patch.object(up.SESSION, "get", side_effect=responses), \
                mock.patch.object(up.time, "sleep", sleeps.append):
            result = up.http_get_json("http://example.test", {})
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(sleeps), 1)  # one backoff between the attempts

    def test_malformed_200_exhausts_retries_and_raises(self):
        """MAX_RETRIES consecutive non-JSON 200s raise a clear error."""
        responses = [FakeResponse(200, json_error=True)
                     for _ in range(up.MAX_RETRIES)]
        with mock.patch.object(up.SESSION, "get", side_effect=responses), \
                mock.patch.object(up.time, "sleep", lambda s: None):
            with self.assertRaisesRegex(RuntimeError, "non-JSON 200"):
                up.http_get_json("http://example.test", {})

    def test_retry_after_over_cap_aborts_immediately(self):
        """A Retry-After demand beyond MAX_WAIT_SECONDS aborts, no sleep."""
        resp = FakeResponse(429, headers={"Retry-After": "28499"})
        sleeps = []
        with mock.patch.object(up.SESSION, "get", return_value=resp), \
                mock.patch.object(up.time, "sleep", sleeps.append):
            with self.assertRaisesRegex(RuntimeError,
                                        "rate limited.*28499.*aborting"):
                up.http_get_json("http://example.test", {})
        self.assertEqual(sleeps, [])  # aborted before any wait

    def test_small_retry_after_is_honored(self):
        """A short Retry-After sleeps then retries."""
        responses = [
            FakeResponse(429, headers={"Retry-After": "3"}),
            FakeResponse(200, json_data={"ok": 1}),
        ]
        sleeps = []
        with mock.patch.object(up.SESSION, "get", side_effect=responses), \
                mock.patch.object(up.time, "sleep", sleeps.append):
            result = up.http_get_json("http://example.test", {})
        self.assertEqual(result, {"ok": 1})
        self.assertEqual(sleeps, [3.0])


class ShouldRequeryNegativeTests(unittest.TestCase):
    TODAY = "2026-07-15"

    def test_never_checked_is_queried(self):
        self.assertTrue(up._should_requery_negative(
            "2010-01-01", None, today=self.TODAY))
        self.assertTrue(up._should_requery_negative(
            "2010-01-01", "", today=self.TODAY))

    def test_recent_publication_is_requeried(self):
        # Published within CROSSREF_RECHECK_PUB_YEARS: always re-query,
        # even if checked yesterday.
        self.assertTrue(up._should_requery_negative(
            "2025-06-01", "2026-07-14", today=self.TODAY))

    def test_old_publication_recent_check_is_skipped(self):
        # Old preprint, checked recently: skip.
        self.assertFalse(up._should_requery_negative(
            "2015-06-01", "2026-07-01", today=self.TODAY))

    def test_old_publication_stale_check_is_requeried(self):
        # Old preprint, last check over CROSSREF_RECHECK_DAYS ago: re-query.
        self.assertTrue(up._should_requery_negative(
            "2015-06-01", "2025-01-01", today=self.TODAY))

    def test_boundary_exactly_365_days_is_skipped(self):
        # 365 days old is not "over" the limit for an old preprint.
        self.assertFalse(up._should_requery_negative(
            "2015-06-01", "2025-07-15", today=self.TODAY))

    def test_missing_pub_date_uses_check_age_only(self):
        self.assertFalse(up._should_requery_negative(
            "", "2026-07-01", today=self.TODAY))
        self.assertTrue(up._should_requery_negative(
            "", "2024-01-01", today=self.TODAY))


class CrossrefCacheFormatTests(unittest.TestCase):
    def test_legacy_flat_format_is_migrated(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "cache.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"10.31234/OSF.IO/ABCDE": "10.1000/published1"}, fh)
            cache = up._load_crossref_cache(path)
        self.assertEqual(
            cache["links"], {"10.31234/osf.io/abcde": "10.1000/published1"})
        self.assertEqual(cache["checked"], {})

    def test_new_format_roundtrip(self):
        cache = {
            "links": {"10.1/a": "10.1/b"},
            "checked": {"10.1/a": "2026-07-15", "10.1/c": "2026-07-15"},
        }
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "cache.json")
            up._save_crossref_cache(path, cache)
            loaded = up._load_crossref_cache(path)
        self.assertEqual(loaded, cache)

    def test_missing_file_yields_empty_cache(self):
        with tempfile.TemporaryDirectory() as td:
            cache = up._load_crossref_cache(os.path.join(td, "nope.json"))
        self.assertEqual(cache, {"links": {}, "checked": {}})


class ExtractTopicTests(unittest.TestCase):
    def test_full_topic(self):
        work = {"primary_topic": {
            "display_name": "Some Topic",
            "field": {"display_name": "Psychology"},
            "subfield": {"display_name": "Cognitive Neuroscience"},
        }}
        self.assertEqual(up.extract_topic(work),
                         ("Psychology", "Cognitive Neuroscience"))

    def test_missing_primary_topic(self):
        self.assertEqual(up.extract_topic({}), ("", ""))
        self.assertEqual(up.extract_topic({"primary_topic": None}), ("", ""))

    def test_partial_topic(self):
        work = {"primary_topic": {"field": {"display_name": "Psychology"},
                                  "subfield": None}}
        self.assertEqual(up.extract_topic(work), ("Psychology", ""))


class ModalValueTests(unittest.TestCase):
    def test_empty_counter_is_blank(self):
        from collections import Counter
        self.assertEqual(gs.modal_value(Counter()), "")

    def test_clear_winner(self):
        from collections import Counter
        c = Counter({"Psychology": 3, "Neuroscience": 1})
        self.assertEqual(gs.modal_value(c), "Psychology")

    def test_tie_breaks_alphabetically(self):
        from collections import Counter
        c = Counter({"Neuroscience": 2, "Computer Science": 2})
        self.assertEqual(gs.modal_value(c), "Computer Science")

    def test_deterministic_regardless_of_insertion_order(self):
        from collections import Counter
        c1 = Counter()
        c1["B Field"] += 1
        c1["A Field"] += 1
        c2 = Counter()
        c2["A Field"] += 1
        c2["B Field"] += 1
        self.assertEqual(gs.modal_value(c1), gs.modal_value(c2))
        self.assertEqual(gs.modal_value(c1), "A Field")


if __name__ == "__main__":
    unittest.main()
