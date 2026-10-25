import gzip
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from paper_digest.weekly.cli import load
from paper_digest.weekly.sources import Collector, RequestBudgetExceeded
from tests.test_weekly import ROOT, paper


class CoverageTests(unittest.TestCase):
    def collector(self, folder, source):
        cfg = load(ROOT / "weekly.example.toml")
        cfg["sources"] = [source]
        return Collector(
            cfg, Path(folder), datetime(2026, 10, 25, tzinfo=ZoneInfo("Asia/Shanghai"))
        )

    def test_crossref_uses_configured_qualifiers_instead_of_fixed_subset(self):
        from paper_digest.weekly.sources import queries

        cfg = load(ROOT / "weekly.example.toml")
        qs = [q for channel, q in queries(cfg, "crossref") if channel == "C"]
        for method in cfg["search"]["methods"]:
            for qualifier in cfg["search"]["qualifiers"]:
                self.assertIn(method + " " + qualifier, qs)
        cfg["search"]["qualifiers"] = ["custom reliability"]
        self.assertEqual(
            len([q for channel, q in queries(cfg, "crossref") if channel == "C"]),
            len(cfg["search"]["methods"]),
        )

    def test_fair_queries_do_not_starve_channel_c(self):
        with tempfile.TemporaryDirectory() as d:
            c = self.collector(d, "crossref")
            calls = []

            def pages(source, query, *args):
                calls.append(query)
                yield [paper()], True
                raise RequestBudgetExceeded("bounded")

            with (
                patch(
                    "paper_digest.weekly.sources.queries",
                    return_value=[("A", "a"), ("B", "b"), ("C", "c")],
                ),
                patch.object(c, "pages", side_effect=pages),
            ):
                c.collect("2026-07-27", "2026-10-25")
            self.assertEqual(calls, ["a", "b", "c"])
            self.assertTrue(all(r["count"] == 1 for r in c.receipts))
            self.assertTrue(all(r["status"] == "partial" for r in c.receipts))

    def test_openalex_rechecks_backfill_for_late_indexed_papers(self):
        with tempfile.TemporaryDirectory() as d:
            c = self.collector(d, "openalex")
            starts = []

            def pages(source, query, start, end):
                starts.append(start)
                yield [], False

            with patch.object(c, "pages", side_effect=pages):
                c.collect("2026-07-27", "2026-10-25", {"openalex": "2026-09-25"})
            self.assertEqual(starts, ["2026-07-27"] * 3)

    def test_anonymous_openalex_pages_and_size_are_legal(self):
        with tempfile.TemporaryDirectory() as d:
            c = self.collector(d, "openalex")
            c.cfg["search"]["page_size"] = 200
            c.cfg["search"]["source_limits"]["openalex"]["max_pages"] = 2
            seen = []

            def get(base, params, source):
                seen.append(params.copy())
                return json.dumps(
                    {"results": [], "meta": {"count": 250}}
                ).encode(), "test.raw.gz"

            with (
                patch.dict("os.environ", {}, clear=True),
                patch.object(c, "get", side_effect=get),
            ):
                pages = list(c.pages("openalex", "query", "2026-07-27", "2026-10-25"))
            self.assertEqual([x["page"] for x in seen], [1, 2])
            self.assertTrue(
                all(x["per-page"] == 100 and "cursor" not in x for x in seen)
            )
            self.assertTrue(pages[-1][1])

    def test_keyed_openalex_follows_server_cursor(self):
        with tempfile.TemporaryDirectory() as d:
            c = self.collector(d, "openalex")
            seen = []

            def get(base, params, source):
                seen.append(params.copy())
                return json.dumps(
                    {"results": [], "meta": {"count": 150, "next_cursor": "cursor2"}}
                ).encode(), "test"

            with (
                patch.dict("os.environ", {"OPENALEX_API_KEY": "test"}),
                patch.object(c, "get", side_effect=get),
            ):
                list(c.pages("openalex", "q", "2026-07-27", "2026-10-25"))
            self.assertEqual([x["cursor"] for x in seen], ["*", "cursor2"])

    def test_biorxiv_uses_total_and_goes_past_old_three_page_limit(self):
        with tempfile.TemporaryDirectory() as d:
            c = self.collector(d, "biorxiv")
            seen = []

            def get(base, source):
                seen.append(base)
                return json.dumps(
                    {"messages": [{"status": "ok", "total": "91"}], "collection": []}
                ).encode(), "test"

            with patch.object(c, "get", side_effect=get):
                pages = list(c.pages("biorxiv", "q", "2026-07-27", "2026-10-25"))
            self.assertEqual(
                [x.rsplit("/", 1)[-1] for x in seen], ["0", "30", "60", "90"]
            )
            self.assertFalse(pages[-1][1])

    def test_crossref_index_and_publication_filters_are_separate(self):
        with tempfile.TemporaryDirectory() as d:
            c = self.collector(d, "crossref")
            seen = []

            def get(base, params, source):
                seen.append(params)
                return b'{"message":{"items":[],"total-results":0}}', "test"

            with patch.object(c, "get", side_effect=get):
                list(c.pages("crossref", "q", "2026-09-25", "2026-10-25"))
            self.assertIn("from-index-date:2026-09-25", seen[0]["filter"])
            self.assertIn("from-pub-date:2026-07-27", seen[0]["filter"])

    def test_compressed_raw_evidence_is_lossless_and_key_not_logged(self):
        with tempfile.TemporaryDirectory() as d:
            c = self.collector(d, "openalex")
            with patch(
                "paper_digest.weekly.sources.fetch_bytes_with_retry",
                return_value=b'{"evidence":true}',
            ):
                raw, path = c.get(
                    "https://api.openalex.org/works",
                    {"api_key": "secret", "page": 1},
                    "openalex",
                )
            self.assertEqual(gzip.decompress((Path(d) / path).read_bytes()), raw)
            self.assertNotIn(
                "secret", next((Path(d) / "evidence").glob("*.json")).read_text()
            )
