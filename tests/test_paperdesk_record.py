"""Persistence is required before live paper trading may react to a scan."""

import gzip
import json
import copy
from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from paperdesk.engine import PaperEngine
from paperdesk.feed import DemoFeed
from paperdesk.record import read_record, record_scan
from paperdesk.server import capture_and_apply


class ScanRecordTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.feed = DemoFeed()

    def test_append_only_round_trip_and_checksum_detects_tampering(self):
        scan = self.feed.scan()
        scan["complete"] = False
        scan["errors"] = [{"reason": "PARTIAL_SNAPSHOT"}]
        first = record_scan(scan, self.root / "scans")
        second = record_scan(scan, self.root / "scans")
        self.assertNotEqual(first, second)
        self.assertEqual(len(list((self.root / "scans").glob("*.json.gz"))), 2)
        archived = read_record(first)
        for key in ("source", "at", "universe", "received", "usable", "complete",
                    "regular", "rows", "benchmarks", "errors", "synthetic"):
            self.assertEqual(archived[key], scan[key])
        self.assertEqual(archived["schemaVersion"], 1)
        self.assertIn("recordedAt", archived)
        self.assertEqual(archived["rows"][0]["code"], scan["rows"][0]["code"])
        body = json.loads(gzip.decompress(first.read_bytes()).decode("utf-8"))
        body["received"] = 0
        first.write_bytes(gzip.compress(json.dumps(body).encode("utf-8"), mtime=0))
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            read_record(first)
        self.assertEqual(read_record(second)["received"], scan["received"])

    def test_rejects_ambiguous_or_nonserializable_observation(self):
        scan = self.feed.scan()
        scan["at"] = "2026-10-17T09:30:00"
        with self.assertRaisesRegex(ValueError, "timezone"):
            record_scan(scan, self.root / "scans")
        self.assertFalse((self.root / "scans").exists())
        scan["at"] = "2026-10-17T09:30:00+00:00"
        scan["rows"][0]["lastPrice"] = float("nan")
        with self.assertRaises(ValueError):
            record_scan(scan, self.root / "scans")
        self.assertFalse((self.root / "scans").exists())

    def test_filename_collision_does_not_replace_existing_record(self):
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)

        scan = self.feed.scan()
        with patch("paperdesk.record.datetime", FixedDateTime), patch(
                "paperdesk.record.uuid.uuid4", return_value=SimpleNamespace(hex="a" * 32)):
            first = record_scan(scan, self.root / "scans")
            original = first.read_bytes()
            with self.assertRaises(FileExistsError):
                record_scan(scan, self.root / "scans")
        self.assertEqual(first.read_bytes(), original)
        self.assertEqual(len(list((self.root / "scans").iterdir())), 1)

    def test_live_archive_failure_disarms_bot_without_a_paper_fill(self):
        engine = PaperEngine(self.root / "live.json", demo=False)
        engine.control("start")
        with patch("paperdesk.server.record_scan", side_effect=OSError("disk full")):
            self.assertFalse(capture_and_apply(self.feed, engine, self.root / "scans"))
        state = engine.state()
        self.assertFalse(state["running"])
        self.assertEqual(state["gate"], "RECORD_ERROR")
        self.assertIn("disk full", state["error"])
        self.assertFalse(state["account"]["trades"])
        self.assertFalse((self.root / "live.json").exists())

    def test_feed_failure_disarms_bot(self):
        engine = PaperEngine(self.root / "live.json", demo=False)
        engine.control("start")
        with patch.object(self.feed, "scan", side_effect=RuntimeError("quote unavailable")):
            self.assertFalse(capture_and_apply(self.feed, engine, self.root / "scans"))
        state = engine.state()
        self.assertFalse(state["running"])
        self.assertEqual(state["gate"], "FEED_ERROR")
        self.assertFalse(state["account"]["trades"])

    def test_incomplete_live_scan_is_archived_before_engine_blocks_trade(self):
        engine = PaperEngine(self.root / "live.json", demo=False)
        engine.control("start")
        incomplete = self.feed.scan()
        incomplete["complete"] = False
        incomplete["synthetic"] = False

        class OneScanFeed:
            def scan(self):
                return incomplete

        self.assertTrue(capture_and_apply(OneScanFeed(), engine, self.root / "scans"))
        [record] = list((self.root / "scans").glob("*.json.gz"))
        self.assertFalse(read_record(record)["complete"])
        self.assertEqual(engine.state()["gate"], "INCOMPLETE_OR_STALE_MARKET_SCAN")
        self.assertFalse(engine.state()["account"]["trades"])

    def test_demo_scan_does_not_write_live_archive(self):
        engine = PaperEngine(self.root / "demo.json", demo=True)
        self.assertTrue(capture_and_apply(self.feed, engine, self.root / "scans"))
        self.assertFalse((self.root / "scans").exists())

    def test_archived_live_signal_fills_only_after_second_archived_scan(self):
        engine = PaperEngine(self.root / "live.json", demo=False)
        engine.control("start")
        first_at = datetime.now(timezone.utc) - timedelta(seconds=3)

        def live_scan(at):
            result = self.feed.scan()
            result.update(source="LOCAL_MOOMOO_OPEND", synthetic=False, at=at.isoformat())
            for quote in result["rows"]:
                price = quote["lastPrice"]
                quote.update(sourceAsOf=at.isoformat(), bidPrice=price * .9995,
                             askPrice=price * 1.0005, bboValid=True)
            result["benchmarks"] = {
                code: {"changePct": 1.0, "lastPrice": 500.0,
                       "sourceAsOf": at.isoformat(), "usable": True, "fresh": True,
                       "bidPrice": 499.95, "askPrice": 500.05, "bboValid": True}
                for code in ("SPY", "QQQ")}
            return result

        class Feed:
            def __init__(self, snapshots):
                self.snapshots = iter(snapshots)

            def scan(self):
                return next(self.snapshots)

        first = live_scan(first_at)
        second = copy.deepcopy(first)
        second_at = (first_at + timedelta(seconds=2)).isoformat()
        second["at"] = second_at
        for quote in second["rows"]:
            quote["sourceAsOf"] = second_at
        for quote in second["benchmarks"].values():
            quote["sourceAsOf"] = second_at
        feed = Feed([first, second])
        self.assertTrue(capture_and_apply(feed, engine, self.root / "scans"))
        self.assertEqual(len(engine.account["pending"]), 3)
        self.assertFalse(engine.account["trades"])
        self.assertTrue(capture_and_apply(feed, engine, self.root / "scans"))
        self.assertEqual(len(engine.account["positions"]), 3)
        self.assertEqual(len(engine.account["trades"]), 3)
        records = list((self.root / "scans").glob("*.json.gz"))
        self.assertEqual(len(records), 2)
        self.assertTrue(all(read_record(path)["source"] == "LOCAL_MOOMOO_OPEND"
                            for path in records))


if __name__ == "__main__":
    unittest.main()
