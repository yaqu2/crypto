"""Cross-check independent paper and offline scan replay implementations."""

from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

from paperdesk.record import record_scan
from tools.replay_parity import compare_scans, main


BASE = datetime(2026, 10, 16, 14, 0, tzinfo=timezone.utc)


def scan(at, price=100, *, complete=True, candidates=1):
    stamp = at.isoformat()
    rows = []
    for i in range(2000):
        chosen = i < candidates
        value = price if chosen else 10
        rows.append({"code": f"US.TEST{i:04d}", "lastPrice": value,
                     "sourceAsOf": stamp, "changePct": 2 if chosen else .5,
                     "turnover": 10_000_000 if chosen else 1_000_000,
                     "bidPrice": value * .9995, "askPrice": value * 1.0005,
                     "bboValid": True, "usable": True, "fresh": True})
    bench = {"sourceAsOf": stamp, "lastPrice": 500, "changePct": 1,
             "bidPrice": 499.95, "askPrice": 500.05, "bboValid": True,
             "usable": True, "fresh": True}
    return {"source": "LOCAL_MOOMOO_OPEND", "at": stamp, "universe": 2000,
            "received": 2000, "usable": 2000, "complete": complete,
            "regular": True, "rows": rows,
            "benchmarks": {"SPY": bench, "QQQ": dict(bench)},
            "errors": [], "synthetic": False}


class ReplayParityTest(unittest.TestCase):
    def test_hashed_archive_cli_checks_real_mode_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            record_scan(scan(BASE), directory)
            record_scan(scan(BASE + timedelta(seconds=90), 102), directory)
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main([str(directory), "--max-scans", "2"])
            result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "PARITY_CHECKED")
        self.assertEqual(result["archiveRecordsAvailable"], 2)

    def test_ordinary_entry_and_stop_rounds_are_checked(self):
        report = compare_scans([scan(BASE), scan(BASE + timedelta(seconds=90), 102),
                                scan(BASE + timedelta(seconds=180), 96),
                                scan(BASE + timedelta(seconds=270), 95)])
        self.assertEqual(report["status"], "PARITY_CHECKED", report["rounds"])
        self.assertEqual(report["rounds"][-1]["paperTrades"], 2)
        self.assertEqual(report["rounds"][-1]["offlineTrades"], 2)

    def test_future_quote_source_is_rejected_consistently(self):
        first = scan(BASE)
        first["rows"][0]["sourceAsOf"] = (BASE + timedelta(seconds=3)).isoformat()
        report = compare_scans([first, scan(BASE + timedelta(seconds=90))])
        self.assertEqual(report["status"], "PARITY_CHECKED", report["rounds"])
        self.assertEqual(report["rounds"][0]["paperGate"],
                         "INCOMPLETE_OR_STALE_MARKET_SCAN")
        self.assertEqual(report["rounds"][1]["paperTrades"], 0)
        self.assertEqual(report["rounds"][1]["offlineTrades"], 0)

    def test_incomplete_next_round_cancels_pending_entry(self):
        report = compare_scans([scan(BASE), scan(BASE + timedelta(seconds=90),
                                                complete=False)])
        self.assertEqual(report["status"], "PARITY_CHECKED", report["rounds"])
        self.assertEqual(report["rounds"][-1]["paperPending"], 0)
        self.assertEqual(report["rounds"][-1]["offlineTrades"], 0)

    def test_reject_after_position_preserves_position_and_cancels_pending_exit(self):
        third = scan(BASE + timedelta(seconds=180), 96)
        fourth = scan(BASE + timedelta(seconds=270), 95, complete=False)
        fifth = scan(BASE + timedelta(seconds=360), 95)
        report = compare_scans([scan(BASE), scan(BASE + timedelta(seconds=90), 102),
                                third, fourth, fifth])
        self.assertEqual(report["rounds"][3]["paperPending"], 0)
        self.assertEqual(report["rounds"][3]["paperPositions"], 1)
        self.assertEqual(report["rounds"][3]["offlinePositions"], 1)
        self.assertEqual(report["rounds"][3]["paperTrades"], 1)
        self.assertEqual(report["rounds"][3]["offlineTrades"], 1)
        self.assertEqual(report["status"], "PARITY_CHECKED", report["rounds"])
        self.assertEqual(report["rounds"][3]["paperEquityDollars"], 98929.10)
        self.assertEqual(report["rounds"][3]["offlineEquityDollars"], 98929.10)

    def test_overnight_position_and_rounding_are_quantified(self):
        next_day = BASE + timedelta(days=3)
        report = compare_scans([scan(BASE), scan(BASE + timedelta(seconds=90), 102),
                                scan(next_day, 90), scan(next_day + timedelta(seconds=90), 91)])
        self.assertEqual(report["scanCount"], 4)
        self.assertEqual(report["rounds"][2]["paperPositions"], 1)
        self.assertEqual(report["rounds"][2]["offlinePositions"], 1)
        self.assertEqual(report["rounds"][2]["paperTrades"], 1)
        self.assertEqual(report["rounds"][2]["offlineTrades"], 1)
        self.assertEqual(report["status"], "PARITY_CHECKED", report["rounds"])
        self.assertIn("paperDayStartEquityDollars", report["rounds"][2])
        self.assertIn("equityDifferenceDollars", report["rounds"][2])
        self.assertIn("paperFeesDollars", report["rounds"][3])
        self.assertIn("offlineFeesDollars", report["rounds"][3])

    def test_portfolio_peak_drawdown_review_is_compared(self):
        report = compare_scans([scan(BASE, candidates=3),
                                scan(BASE + timedelta(seconds=90), 100, candidates=3),
                                scan(BASE + timedelta(seconds=180), 65, candidates=3),
                                scan(BASE + timedelta(seconds=270), 66, candidates=3)])
        self.assertEqual(report["rounds"][1]["paperPositions"], 3)
        self.assertTrue(report["rounds"][2]["paperRiskReviewRequired"])
        self.assertTrue(report["rounds"][2]["offlineRiskReviewRequired"])
        self.assertFalse(report["rounds"][2]["paperRunning"])
        self.assertEqual(report["rounds"][2]["paperPending"], 0)
        self.assertEqual(report["rounds"][3]["paperTrades"], 3)
        self.assertEqual(report["rounds"][3]["offlineTrades"], 3)
        self.assertEqual(report["rounds"][2]["paperRiskReviewAt"],
                         report["rounds"][2]["offlineRiskReviewAt"])
        self.assertEqual(report["status"], "PARITY_CHECKED", report["rounds"])


if __name__ == "__main__":
    unittest.main()
