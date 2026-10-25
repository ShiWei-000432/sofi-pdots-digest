"""Safety-critical weekly regressions; network and SMTP fixtures are synthetic."""

import os
import smtplib
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from paper_digest.weekly.cli import due, load
from paper_digest.weekly.delivery import send
from paper_digest.weekly.model import date_parts, merge, rank, recent
from paper_digest.weekly.provider import SUMMARY, ProviderError, validate_summary
from paper_digest.weekly.report import write_report
from paper_digest.weekly.sources import Collector, basic_record, pubmed_record, queries
from paper_digest.weekly.state import Store

ROOT = Path(__file__).resolve().parents[1]


def paper(
    title="Balanced SOFI fluorescence cumulant background correction",
    doi="10.1234/test",
):
    p = basic_record(
        title,
        "We use fluorescence imaging.",
        doi,
        "2026-10-20",
        "crossref",
        ["A"],
        "Journal",
    )
    p.update(
        category="直接相关",
        score=80,
        score_reasons=["cumulant"],
        first_seen_at="2026-10-25",
        screen={"reason": "relevant"},
    )
    return p


class WeeklyTests(unittest.TestCase):
    def test_domain(self):
        self.assertEqual(rank(paper("SOFI financial earnings and banking"))[0], 0)
        self.assertEqual(rank(paper("Carbon dots for cancer therapy"))[0], 0)
        self.assertEqual(rank(paper("Quantum dots fluorescence imaging"))[0], 0)
        self.assertEqual(
            rank(paper("Pdots fluorescence blinking"))[1], "探针与实验支撑"
        )
        self.assertGreater(rank(paper())[0], 0)
        self.assertGreater(
            rank(
                paper("Super-resolution microscopy background correction uncertainty")
            )[0],
            0,
        )

    def test_identity_bridge_and_no_title_merge(self):
        a, b, c = paper(), paper(), paper(doi="10.1234/other")
        a["ids"] = ["doi:10.1234/test"]
        b["ids"] = ["pmid:1"]
        c["ids"] = ["pmid:1", "doi:10.1234/test"]
        self.assertEqual(len(merge([a, b, c])), 1)
        self.assertEqual(len(merge([paper(), paper(doi="10.1234/distinct")])), 2)
        self.assertEqual(
            paper(doi="HTTPS://DOI.ORG/10.1234/TEST")["doi"], "10.1234/test"
        )

    def test_pubmed_dates(self):
        node = ET.fromstring(
            """<PubmedArticle><MedlineCitation><PMID>1</PMID><Article><ArticleTitle>SOFI</ArticleTitle><Journal><JournalIssue><PubDate><Year>2024</Year><Month>Mar</Month></PubDate></JournalIssue></Journal></Article></MedlineCitation><PubmedData><History><PubMedPubDate PubStatus="entrez"><Year>2026</Year><Month>10</Month><Day>24</Day></PubMedPubDate></History></PubmedData></PubmedArticle>"""
        )
        p = pubmed_record(node)
        self.assertEqual(p["published_at"], "2024-03")
        self.assertEqual(p["publication_precision"], "month")
        self.assertTrue(p["indexed_at"].startswith("2026-10-24"))
        self.assertFalse(recent(p, "2026-07-01", "2026-10-25"))
        self.assertEqual(date_parts([2025]), ("2025", "year"))
        self.assertFalse(
            recent({"published_at": "2026-10"}, "2026-10-18", "2026-10-25")
        )

    def test_crossref_publication_not_index_and_partial_precision(self):
        from paper_digest.weekly.sources import crossref_record

        p = crossref_record(
            {
                "DOI": "10.1234/a",
                "title": ["Paper"],
                "indexed": {"date-time": "2026-10-24T00:00:00Z"},
                "published-online": {"date-parts": [[2024, 3]]},
            }
        )
        self.assertEqual(p["published_at"], "2024-03")
        self.assertEqual(p["publication_precision"], "month")
        self.assertTrue(p["indexed_at"].startswith("2026"))
        p = crossref_record(
            {
                "DOI": "10.1234/a",
                "title": ["Paper"],
                "indexed": {"date-time": "2026-10-24T00:00:00Z"},
            }
        )
        self.assertIsNone(p["published_at"])

    def test_carbonized_polymer_dots_not_pdots(self):
        self.assertEqual(
            rank(paper("Fluorescent carbonized polymer dots for imaging"))[0], 0
        )
        self.assertEqual(
            rank(paper("Supplementary document for SOFI fluorescence"))[0], 0
        )

    def test_europepmc_pmid_without_doi(self):
        from paper_digest.weekly.sources import ep_record

        p = ep_record(
            {"id": "1234", "source": "MED", "title": "SOFI", "pubYear": "2025"}
        )
        self.assertIn("pmid:1234", p["ids"])
        self.assertEqual(p["publication_precision"], "year")

    def test_crash_during_data_stays_unsent(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.sqlite"
            store = Store(path)
            store.fetched(paper(), "2026-10-25")
            store.db.execute(
                "INSERT INTO outbox VALUES (?,?,?,?,?)",
                ("id", "<message>", "sending", "{}", None),
            )
            store.db.commit()
            store.close()
            store = Store(path)
            self.assertEqual(
                store.db.execute("SELECT status FROM outbox").fetchone()[0], "uncertain"
            )
            self.assertEqual(len(store.unsent()), 1)
            store.close()

    def test_abstract_evidence(self):
        a = {k: "" for k in SUMMARY["properties"]}
        a.update(
            reported_results=[],
            author_limitations=[],
            recommendation="参考",
            evidence_scope="仅有摘要",
        )
        validate_summary(a, "100 frames were acquired.", "仅有摘要")
        a["reported_results"] = [
            {"text": "用了500帧", "quote": "100 frames were acquired."}
        ]
        with self.assertRaises(ProviderError):
            validate_summary(a, "100 frames were acquired.", "仅有摘要")
        a["reported_results"] = [{"text": "没有证据", "quote": "invented"}]
        with self.assertRaises(ProviderError):
            validate_summary(a, "100 frames were acquired.", "仅有摘要")
        a.update(reported_results=[], evidence_scope="已读全文")
        with self.assertRaises(ProviderError):
            validate_summary(a, "abstract", "仅有摘要")

    def test_partial_source_failure(self):
        cfg = load(ROOT / "weekly.example.toml")
        cfg["sources"] = ["pubmed", "europepmc"]
        with tempfile.TemporaryDirectory() as d:
            c = Collector(cfg, Path(d), datetime.now(ZoneInfo("Asia/Shanghai")))

            def pages(source, *args):
                if source == "pubmed":
                    raise RuntimeError("offline")
                yield [paper()], False

            with patch.object(c, "pages", side_effect=pages):
                result = c.collect("2026-07-01", "2026-10-25")
            self.assertEqual(len(result), 3)
            self.assertEqual(c.receipts[0]["status"], "failed")
            self.assertEqual(c.receipts[-1]["status"], "ok")

    def test_queries_are_source_specific(self):
        cfg = load(ROOT / "weekly.example.toml")
        self.assertIn("[Title/Abstract]", queries(cfg, "pubmed")[0][1])
        self.assertIn("all:", queries(cfg, "arxiv")[0][1])
        self.assertNotIn(" OR ", queries(cfg, "crossref")[0][1])

    def test_catch_up(self):
        cfg = load(ROOT / "weekly.example.toml")
        now = datetime(2026, 10, 25, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertTrue(due(now, cfg, "2026-10-20T09:00:00+08:00"))
        self.assertFalse(due(now, cfg, "2026-10-23T10:00:00+08:00"))

    def test_fetched_is_not_sent_and_alias_persistence(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "state.sqlite")
            p = paper()
            store.fetched(p, "2026-10-25")
            self.assertEqual(len(store.unsent()), 1)
            store.cache("key", {"summary": "ok"}, p["id"], "2026-10-25")
            self.assertEqual(len(store.unsent()), 1)
            store.db.execute("UPDATE papers SET sent_at=?", ("2026-10-25",))
            store.db.commit()
            p["ids"].append("pmid:42")
            store.fetched(p, "2026-10-26")
            self.assertEqual(len(store.unsent()), 0)
            store.close()

    def test_mail_retry_uncertain_and_acceptance(self):
        cfg = load(ROOT / "weekly.example.toml")
        env = dict(
            SMTP_HOST="example.test",
            SMTP_PORT="465",
            SMTP_USERNAME="sender@example.test",
            SMTP_PASSWORD="secret",
            SMTP_FROM="sender@example.test",
            SMTP_SECURITY="ssl",
        )
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, env):
            out = Path(d)
            store = Store(out / "state.sqlite")
            p = paper()
            store.fetched(p, "2026-10-25")
            report = dict(
                date="2026-10-25",
                papers=[],
                complete=False,
                analysis_failures=[],
                sources=[],
                generated_at="2026-10-25",
                fetched_count=1,
                unique_count=1,
            )
            write_report(report, out, cfg["recipient"])
            report["papers"] = [p]
            with patch(
                "paper_digest.weekly.delivery.send_weekly_message",
                side_effect=smtplib.SMTPAuthenticationError(535, b"bad"),
            ):
                with self.assertRaises(RuntimeError):
                    send(cfg, store, report, out)
            self.assertEqual(len(store.unsent()), 1)
            self.assertEqual(
                store.db.execute("SELECT status FROM outbox").fetchone()[0], "retryable"
            )

            def ambiguous(config, message, data, accepted):
                data()
                raise ConnectionResetError()

            with patch(
                "paper_digest.weekly.delivery.send_weekly_message",
                side_effect=ambiguous,
            ):
                with self.assertRaises(RuntimeError):
                    send(cfg, store, report, out)
            self.assertEqual(
                store.db.execute("SELECT status FROM outbox").fetchone()[0], "uncertain"
            )
            with patch("paper_digest.weekly.delivery.send_weekly_message") as mocked:
                with self.assertRaises(RuntimeError):
                    send(cfg, store, report, out)
                mocked.assert_not_called()
            store.db.execute("UPDATE outbox SET status='retryable'")
            store.db.commit()

            def success(config, message, data, accepted):
                data()
                accepted()

            with patch(
                "paper_digest.weekly.delivery.send_weekly_message", side_effect=success
            ) as mocked:
                self.assertEqual(send(cfg, store, report, out), "accepted")
                self.assertEqual(send(cfg, store, report, out), "already_accepted")
                self.assertEqual(mocked.call_count, 1)
            self.assertEqual(len(store.unsent()), 0)
            store.close()

    def test_no_false_empty_claim(self):
        from paper_digest.weekly.report import render

        r = dict(
            date="2026-10-25",
            papers=[],
            complete=False,
            analysis_failures=[],
            sources=[],
            generated_at="2026-10-25",
            fetched_count=0,
            unique_count=0,
        )
        self.assertNotIn("本周未发现重要新成果", render(r))
        r["complete"] = True
        self.assertIn("本周未发现重要新成果", render(r))


if __name__ == "__main__":
    unittest.main()
