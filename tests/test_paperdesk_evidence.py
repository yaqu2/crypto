"""The dashboard must show research evidence separately from paper PnL."""
import json
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import urlopen

from paperdesk.engine import PaperEngine
from paperdesk.feed import DemoFeed
from paperdesk.server import EvidenceReader, make_handler


class EvidencePanelTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.report = self.root / "eval.json"

    def test_missing_or_invalid_report_never_claims_success(self):
        reader = EvidenceReader(self.report)
        self.assertEqual(reader.read()["status"], "NOT_EVALUATED")
        self.report.write_text('{"status":"PROFIT_PROVEN"}', encoding="utf-8")
        self.assertEqual(reader.read()["status"], "INVALID_REPORT")

    def test_no_go_and_research_only_summarize_holdout_without_paper_equity(self):
        reader = EvidenceReader(self.report)
        report = {"status": "NO_GO", "reasons": ["FEWER_THAN_60_REGULAR_TRADING_DAYS"],
                  "sample": {"regularTradingDays": 3, "holdoutTradingDays": 1,
                             "lastAt": "2026-10-16T19:59:00+00:00"},
                  "holdout": {"completedRoundTrips": 2, "netReturnPct": 1.2,
                              "spyNetReturnPct": 0.4},
                  "holdoutStress": {"netReturnPct": 0.2}}
        self.report.write_text(json.dumps(report), encoding="utf-8")
        summary = reader.read()
        self.assertEqual(summary["status"], "NO_GO")
        self.assertEqual(summary["regularDays"], 3)
        self.assertEqual(summary["reasons"], ["FEWER_THAN_60_REGULAR_TRADING_DAYS"])
        report["status"] = "RESEARCH_ONLY"
        self.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
        self.assertEqual(reader.read()["status"], "INVALID_REPORT")
        report["sample"].update(regularTradingDays=80, holdoutTradingDays=24,
                                fullSessionDays=80, completeFreshScanFraction=.99)
        report["train"] = {"completedRoundTrips": 80}
        report["holdout"].update(completedRoundTrips=35, netReturnPct=5.2,
                                 spyNetReturnPct=2.0)
        report["holdoutStress"]["netReturnPct"] = 2.1
        report["reasons"] = []
        report["validationRegistration"] = {"status": "VALID", "splitMethod": "REGISTERED_ET_DATE",
                                            "cutoffEtDate": "2026-10-01", "sha256": "a" * 64}
        report["captureAttempts"] = {"status": "VALID", "regularCompleteScanFraction": .99,
                                     "engineErrors": 0}
        report["continuousRisk"] = {"riskReviewRequired": False}
        self.report.write_text(json.dumps(report, indent=4), encoding="utf-8")
        self.assertEqual(reader.read()["status"], "RESEARCH_ONLY")
        report["continuousRisk"]["riskReviewRequired"] = True
        self.report.write_text(json.dumps(report), encoding="utf-8")
        self.assertEqual(reader.read()["status"], "INVALID_REPORT")

    def test_new_live_scan_marks_preliminary_report_stale(self):
        now = datetime.now(timezone.utc)
        report = {"status": "RESEARCH_ONLY", "reasons": [],
                  "sample": {"regularTradingDays": 80, "holdoutTradingDays": 24,
                             "fullSessionDays": 80, "completeFreshScanFraction": .99,
                             "lastAt": (now - timedelta(minutes=2)).isoformat()},
                  "train": {"completedRoundTrips": 80},
                  "holdout": {"completedRoundTrips": 35, "netReturnPct": 5.2,
                              "spyNetReturnPct": 2.0},
                  "holdoutStress": {"netReturnPct": 2.1},
                  "validationRegistration": {"status": "VALID", "splitMethod": "REGISTERED_ET_DATE",
                                             "cutoffEtDate": "2026-10-01", "sha256": "b" * 64},
                  "captureAttempts": {"status": "VALID", "regularCompleteScanFraction": .99,
                                      "engineErrors": 0},
                  "continuousRisk": {"riskReviewRequired": False}}
        self.report.write_text(json.dumps(report), encoding="utf-8")
        engine = PaperEngine(self.root / "live.json", demo=False)
        engine.scan = {"at": now.isoformat()}
        server = ThreadingHTTPServer(("127.0.0.1", 0),
                                     make_handler(engine, "token", EvidenceReader(self.report)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with urlopen(f"http://127.0.0.1:{server.server_port}/t/token/api/state") as response:
            self.assertEqual(json.load(response)["evidence"]["status"], "STALE_REPORT")

    def test_http_state_includes_unverified_evidence_not_demo_profit(self):
        engine = PaperEngine(self.root / "demo.json", demo=True)
        engine.control("start")
        engine.apply(DemoFeed().scan())
        server = ThreadingHTTPServer(("127.0.0.1", 0),
                                     make_handler(engine, "token", EvidenceReader(self.report)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with urlopen(f"http://127.0.0.1:{server.server_port}/t/token/api/state") as response:
            state = json.load(response)
        self.assertEqual(state["evidence"]["status"], "NOT_EVALUATED")
        self.assertTrue(state["account"]["trades"])


if __name__ == "__main__":
    unittest.main()
