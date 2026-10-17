"""Recorded-snapshot research must never fill at signal time."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from paperdesk.record import record_scan
from quantlab.intraday_eval import _segment, evaluate, evaluate_archive, load_snapshots


def scan(at, candidate_price=100, *, complete=True, synthetic=False):
    stamp = at.isoformat()
    rows = []
    for i in range(2000):
        chosen = i == 0
        rows.append({"code": f"US.TEST{i:04d}", "lastPrice": candidate_price if chosen else 10,
                     "sourceAsOf": stamp, "changePct": 2 if chosen else .5,
                     "turnover": 10_000_000 if chosen else 1_000_000, "spreadBps": 10,
                     "bidPrice": (candidate_price if chosen else 10) * .9995,
                     "askPrice": (candidate_price if chosen else 10) * 1.0005,
                     "bboValid": True, "usable": True, "fresh": True})
    bench = {"sourceAsOf": stamp, "lastPrice": 500, "changePct": 1,
             "bidPrice": 499.95, "askPrice": 500.05, "bboValid": True,
             "usable": True, "fresh": True}
    return {"source": "SYNTHETIC_DEMO" if synthetic else "LOCAL_MOOMOO_OPEND",
            "at": stamp, "universe": 2000, "received": 2000, "usable": 2000,
            "complete": complete, "regular": True, "rows": rows,
            "benchmarks": {"SPY": bench, "QQQ": dict(bench)}, "errors": [],
            "synthetic": synthetic}


class IntradayEvalTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.t0 = datetime(2026, 10, 16, 14, 0, tzinfo=timezone.utc)

    def archive(self, snapshots):
        for item in snapshots:
            record_scan(item, self.directory)
        return load_snapshots(self.directory)

    def test_missing_archive_is_explicit_no_go(self):
        result = evaluate_archive(self.directory / "scans")
        self.assertEqual(result["status"], "NO_GO")
        self.assertEqual(result["reasons"], ["NO_RECORDED_SCANS"])

    def test_signal_fills_on_next_complete_scan_then_exit_one_scan_later(self):
        scans = self.archive([scan(self.t0), scan(self.t0 + timedelta(seconds=90), 102),
                              scan(self.t0 + timedelta(seconds=180), 96),
                              scan(self.t0 + timedelta(seconds=270), 95)])
        result = evaluate(scans)
        segment = _segment(scans)
        trades = segment["trades"]
        buys = [t for t in trades if t["side"] == "BUY"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["signalAt"], self.t0.isoformat())
        self.assertEqual(buys[0]["fillAt"], (self.t0 + timedelta(seconds=90)).isoformat())
        self.assertGreater(buys[0]["fillPrice"], 102)
        self.assertEqual(segment["completedRoundTrips"], 1)
        self.assertEqual(trades[-1]["signalAt"], (self.t0 + timedelta(seconds=180)).isoformat())
        self.assertEqual(trades[-1]["fillAt"], (self.t0 + timedelta(seconds=270)).isoformat())
        self.assertEqual(result["status"], "NO_GO")
        self.assertIn("FEWER_THAN_60_REGULAR_TRADING_DAYS", result["reasons"])
        self.assertIn("FEWER_THAN_100_COMPLETED_ROUND_TRIPS", result["reasons"])

    def test_incomplete_next_scan_cancels_entry(self):
        scans = self.archive([scan(self.t0), scan(self.t0 + timedelta(seconds=90), complete=False)])
        result = evaluate(scans)
        self.assertEqual(result["train"]["trades"], [])
        self.assertEqual(result["holdout"]["trades"], [])
        self.assertIn("COMPLETE_FRESH_SCAN_FRACTION_BELOW_95_PERCENT", result["reasons"])

    def test_missing_next_round_bbo_cancels_entry(self):
        second = scan(self.t0 + timedelta(seconds=90))
        second["rows"][0]["bboValid"] = False
        scans = self.archive([scan(self.t0), second])
        self.assertEqual(_segment(scans)["trades"], [])
        self.assertGreater(_segment(scans)["missedOrCancelledFills"], 0)

    def test_entry_requires_candidate_to_remain_eligible_on_next_scan(self):
        second = scan(self.t0 + timedelta(seconds=90))
        second["rows"][0]["changePct"] = 0.5
        scans = self.archive([scan(self.t0), second])
        self.assertEqual(_segment(scans)["trades"], [])

    def test_pending_entry_may_fill_when_candidate_falls_below_display_top_twelve(self):
        second = scan(self.t0 + timedelta(seconds=90))
        for row in second["rows"][1:13]:
            row["changePct"] = 8
            row["turnover"] = 10_000_000
        scans = self.archive([scan(self.t0), second])
        self.assertEqual([trade["code"] for trade in _segment(scans)["trades"]], ["US.TEST0000"])

    def test_declared_spread_cannot_override_wide_displayed_bbo(self):
        first = scan(self.t0)
        first["rows"][0]["bidPrice"] = 99.5
        first["rows"][0]["askPrice"] = 100.5
        # A stale or inaccurate spreadBps field must not make this eligible.
        first["rows"][0]["spreadBps"] = 10
        scans = self.archive([first, scan(self.t0 + timedelta(seconds=90))])
        self.assertEqual(_segment(scans)["trades"], [])

    def test_displayed_last_outside_bbo_range_is_not_candidate(self):
        first = scan(self.t0)
        first["rows"][0]["bidPrice"] = 50
        first["rows"][0]["askPrice"] = 50.01
        scans = self.archive([first, scan(self.t0 + timedelta(seconds=90))])
        self.assertEqual(_segment(scans)["trades"], [])

    def test_exit_stop_uses_bid_even_when_last_price_has_not_fallen(self):
        third = scan(self.t0 + timedelta(seconds=180), 102)
        third["rows"][0]["bidPrice"] = 98
        scans = self.archive([scan(self.t0), scan(self.t0 + timedelta(seconds=90), 102),
                              third, scan(self.t0 + timedelta(seconds=270), 99)])
        trades = _segment(scans)["trades"]
        self.assertEqual([trade["side"] for trade in trades], ["BUY", "SELL"])
        self.assertEqual(trades[-1]["signalAt"], third["at"])

    def test_repeated_quote_timestamp_cannot_fill_after_signal(self):
        second = scan(self.t0 + timedelta(seconds=90))
        second["rows"][0]["sourceAsOf"] = self.t0.isoformat()
        scans = self.archive([scan(self.t0), second])
        self.assertEqual(_segment(scans)["trades"], [])

    def test_holdout_has_independent_trade_and_result_gates(self):
        scans = self.archive([scan(self.t0), scan(self.t0 + timedelta(seconds=90))])
        result = evaluate(scans)
        self.assertIn("FEWER_THAN_30_HOLDOUT_ROUND_TRIPS", result["reasons"])
        self.assertIn("HOLDOUT_NOT_PROFITABLE_AFTER_COSTS", result["reasons"])
        self.assertIn("HOLDOUT_NOT_PROFITABLE_AT_DOUBLE_COSTS", result["reasons"])

    def test_double_cost_stress_reduces_same_strategy_net_return(self):
        scans = self.archive([scan(self.t0), scan(self.t0 + timedelta(seconds=90), 102),
                              scan(self.t0 + timedelta(seconds=180), 108),
                              scan(self.t0 + timedelta(seconds=270), 108)])
        base = _segment(scans)
        stress = _segment(scans, slippage_bps=20, fee_bps=4)
        self.assertEqual(base["completedRoundTrips"], 1)
        self.assertEqual(stress["completedRoundTrips"], 1)
        self.assertGreater(base["netReturnPct"], stress["netReturnPct"])

    def test_closed_market_scans_do_not_dilute_regular_coverage(self):
        closed = scan(self.t0 - timedelta(hours=2))
        closed["regular"] = False
        scans = self.archive([closed, scan(self.t0), scan(self.t0 + timedelta(seconds=90))])
        result = evaluate(scans)
        self.assertEqual(result["sample"]["regularSnapshots"], 2)
        self.assertEqual(result["sample"]["excludedNonRegularScans"], 1)
        self.assertEqual(result["sample"]["completeFreshScanFraction"], 1.0)

    def test_synthetic_rejected_even_when_schema_and_checksum_are_valid(self):
        record_scan(scan(self.t0, synthetic=True), self.directory)
        with self.assertRaisesRegex(ValueError, "synthetic or unverified"):
            load_snapshots(self.directory)

    def test_archive_checksum_tamper_rejected(self):
        file = record_scan(scan(self.t0), self.directory)
        file.write_bytes(file.read_bytes() + b"x")
        with self.assertRaisesRegex(ValueError, "checksum"):
            load_snapshots(self.directory)

    def test_streaming_archive_matches_small_in_memory_result(self):
        scans = self.archive([scan(self.t0), scan(self.t0 + timedelta(seconds=90), 102),
                              scan(self.t0 + timedelta(seconds=180), 108),
                              scan(self.t0 + timedelta(seconds=270), 108)])
        expected = evaluate(scans)
        actual = evaluate_archive(self.directory)
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual["sample"]["archiveSha256List"]), 4)


if __name__ == "__main__":
    unittest.main()
