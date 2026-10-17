"""Recorded-snapshot research must never fill at signal time."""

from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paperdesk.record import record_scan
from quantlab.intraday_eval import _segment, evaluate, evaluate_archive, load_snapshots, register_archive


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

    def register(self, cutoff):
        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                fixed = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
                return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

        with patch("quantlab.intraday_eval.datetime", FrozenDatetime):
            return register_archive(self.directory, cutoff)

    def attempt(self, attempt_id, at, phase, *, scan_file=None, exception_type=None):
        event = {"schemaVersion": 1, "attemptId": f"{attempt_id:032x}",
                 "startedAt": at.isoformat(), "recordedAt": at.isoformat(), "phase": phase}
        if scan_file is not None:
            event["scanFile"] = scan_file
        if exception_type is not None:
            event["exceptionType"] = exception_type
        raw = gzip.compress((json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode(), mtime=0)
        folder = self.directory / "attempts"
        folder.mkdir(exist_ok=True)
        (folder / f"{attempt_id}-{hashlib.sha256(raw).hexdigest()}.json.gz").write_bytes(raw)

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

    def test_incomplete_scan_marks_held_position_without_trading(self):
        scans = self.archive([scan(self.t0), scan(self.t0 + timedelta(seconds=90), 102),
                              scan(self.t0 + timedelta(seconds=180), 70, complete=False)])
        segment = _segment(scans)
        self.assertEqual([trade["side"] for trade in segment["trades"]], ["BUY"])
        self.assertLess(segment["equityCurve"][-1]["equity"],
                        segment["equityCurve"][-2]["equity"] - 1000)
        self.assertEqual(segment["staleHeldMarks"], 0)

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
        expected["validationRegistration"]["path"] = str(self.directory / "validation.lock")
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual["sample"]["archiveSha256List"]), 4)

    def test_missing_registration_and_attempt_log_force_no_go(self):
        self.archive([scan(self.t0), scan(self.t0 + timedelta(seconds=90))])
        result = evaluate_archive(self.directory)
        self.assertIn("MISSING_VALIDATION_REGISTRATION", result["reasons"])
        self.assertIn("MISSING_CAPTURE_ATTEMPT_LOG", result["reasons"])
        self.assertEqual(result["validationRegistration"]["splitMethod"], "EXPLORATORY_70_30")

    def test_registered_holdout_date_stays_fixed_as_new_days_arrive(self):
        self.archive([scan(self.t0), scan(self.t0 + timedelta(seconds=90))])
        lock = self.register("2026-10-19")
        self.assertEqual(lock["cutoffEtDate"], "2026-10-19")
        monday = self.t0 + timedelta(days=3)
        self.archive([scan(monday), scan(monday + timedelta(seconds=90))])
        result = evaluate_archive(self.directory)
        self.assertEqual(result["validationRegistration"]["status"], "VALID")
        self.assertEqual(result["sample"]["holdoutStart"], monday.isoformat())
        self.archive([scan(monday + timedelta(days=1))])
        later = evaluate_archive(self.directory)
        self.assertEqual(later["sample"]["holdoutStart"], monday.isoformat())
        self.assertEqual(later["validationRegistration"]["splitMethod"], "REGISTERED_ET_DATE")
        self.assertEqual(later["validationRegistration"]["sha256"],
                         hashlib.sha256((self.directory / "validation.lock").read_bytes()).hexdigest())

    def test_registration_rejects_changed_strategy_or_training_archive(self):
        self.archive([scan(self.t0), scan(self.t0 + timedelta(seconds=90))])
        self.register("2026-10-19")
        with self.assertRaises(FileExistsError):
            self.register("2026-10-20")
        lock_file = self.directory / "validation.lock"
        original = lock_file.read_bytes()
        changed = json.loads(original)
        changed["strategyCode"]["paperEngineSha256"] = "0" * 64
        lock_file.write_text(json.dumps(changed), encoding="utf-8")
        result = evaluate_archive(self.directory)
        self.assertIn("REGISTRATION_STRATEGY_CODE_MISMATCH", result["reasons"])
        lock_file.write_bytes(original)
        self.archive([scan(self.t0 - timedelta(seconds=90))])
        result = evaluate_archive(self.directory)
        self.assertIn("REGISTRATION_TRAIN_ARCHIVE_MISMATCH", result["reasons"])

    def test_registration_detects_late_training_attempt_event(self):
        self.archive([scan(self.t0)])
        self.register("2026-10-19")
        self.attempt(1, self.t0, "STARTED")
        result = evaluate_archive(self.directory)
        self.assertIn("REGISTRATION_TRAIN_ATTEMPT_LOG_MISMATCH", result["reasons"])

    def test_failed_regular_attempt_counts_against_capture_coverage(self):
        scans = self.archive([scan(self.t0)])
        self.attempt(1, self.t0, "STARTED")
        self.attempt(1, self.t0, "SCAN_ARCHIVED", scan_file=scans[0]["_fileName"])
        self.attempt(2, self.t0 + timedelta(seconds=90), "STARTED")
        self.attempt(2, self.t0 + timedelta(seconds=90), "FEED_ERROR", exception_type="ConnectionError")
        self.register("2026-10-19")
        result = evaluate_archive(self.directory)
        self.assertEqual(result["captureAttempts"]["failedAttempts"], 1)
        self.assertEqual(result["captureAttempts"]["regularAttempts"], 2)
        self.assertEqual(result["captureAttempts"]["regularCompleteScanFraction"], .5)
        self.assertIn("CAPTURE_ATTEMPT_COVERAGE_BELOW_95_PERCENT", result["reasons"])

    def test_successful_attempts_match_all_scans_and_offhours_failure_is_reported(self):
        scans = self.archive([scan(self.t0), scan(self.t0 + timedelta(seconds=90))])
        for index, item in enumerate(scans, 1):
            self.attempt(index, item["_at"], "STARTED")
            self.attempt(index, item["_at"], "SCAN_ARCHIVED", scan_file=item["_fileName"])
        weekend = self.t0 + timedelta(days=1)
        self.attempt(3, weekend, "STARTED")
        self.attempt(3, weekend, "FEED_ERROR", exception_type="TimeoutError")
        self.register("2026-10-19")
        result = evaluate_archive(self.directory)
        self.assertEqual(result["captureAttempts"]["status"], "VALID")
        self.assertEqual(result["captureAttempts"]["totalAttempts"], 3)
        self.assertEqual(result["captureAttempts"]["failedAttempts"], 1)
        self.assertEqual(result["captureAttempts"]["regularAttempts"], 2)
        self.assertEqual(result["captureAttempts"]["regularCompleteScanFraction"], 1.0)
        self.assertNotIn("CAPTURE_ATTEMPT_COVERAGE_BELOW_95_PERCENT", result["reasons"])

    def test_attempt_started_during_regular_hours_counts_when_scan_finishes_after_close(self):
        near_close = self.t0.replace(hour=19, minute=59)
        item = scan(near_close + timedelta(minutes=1))
        item["regular"] = False
        scans = self.archive([item])
        self.attempt(1, near_close, "STARTED")
        self.attempt(1, near_close, "SCAN_ARCHIVED", scan_file=scans[0]["_fileName"])
        result = evaluate_archive(self.directory)
        self.assertEqual(result["captureAttempts"]["regularAttempts"], 1)
        self.assertEqual(result["captureAttempts"]["regularCompleteScanFraction"], 0.0)

    def test_corrupted_attempt_event_checksum_is_no_go(self):
        self.archive([scan(self.t0)])
        self.attempt(1, self.t0, "STARTED")
        file = next((self.directory / "attempts").glob("*.json.gz"))
        file.write_bytes(file.read_bytes() + b"x")
        self.assertIn("INVALID_CAPTURE_ATTEMPT_LOG", evaluate_archive(self.directory)["reasons"])

    def test_orphan_scan_and_engine_error_block_research_pass(self):
        scans = self.archive([scan(self.t0)])
        self.attempt(1, self.t0, "STARTED")
        result = evaluate_archive(self.directory)
        self.assertIn("INCOMPLETE_OR_CONFLICTING_CAPTURE_ATTEMPT_LOG", result["reasons"])
        self.assertIn("CAPTURE_ATTEMPT_SCAN_MISMATCH", result["reasons"])
        self.attempt(1, self.t0, "SCAN_ARCHIVED", scan_file=scans[0]["_fileName"])
        self.attempt(1, self.t0, "ENGINE_ERROR", exception_type="RuntimeError")
        result = evaluate_archive(self.directory)
        self.assertEqual(result["captureAttempts"]["engineErrors"], 1)
        self.assertIn("PAPER_ENGINE_EXECUTION_ERRORS", result["reasons"])

    def test_drawdown_review_freezes_future_orders(self):
        snapshots = [scan(self.t0 + timedelta(seconds=90 * i)) for i in range(4)]
        for item in snapshots:
            for row in item["rows"][:3]:
                row.update(lastPrice=100, bidPrice=99.95, askPrice=100.05,
                           changePct=2, turnover=10_000_000)
        for row in snapshots[2]["rows"][:3]:
            row.update(lastPrice=70, bidPrice=69.95, askPrice=70.05)
        scans = self.archive(snapshots)
        segment = _segment(scans)
        self.assertEqual([trade["side"] for trade in segment["trades"]], ["BUY"] * 3)
        self.assertTrue(segment["riskReviewRequired"])
        self.assertEqual(segment["riskReviewAt"], snapshots[2]["at"])
        self.assertEqual(segment["openPositions"], 3)
        lock = self.register("2026-10-19")
        self.assertIn("PORTFOLIO_DRAWDOWN_REVIEW_REQUIRED", evaluate(scans, lock)["reasons"])

    def test_continuous_risk_remains_latched_across_registered_cutoff(self):
        friday = [scan(self.t0), scan(self.t0 + timedelta(seconds=90))]
        for item in friday:
            for row in item["rows"][:3]:
                row.update(lastPrice=100, bidPrice=99.95, askPrice=100.05,
                           changePct=2, turnover=10_000_000)
        self.archive(friday)
        self.register("2026-10-19")
        monday_at = self.t0 + timedelta(days=3)
        monday = scan(monday_at)
        for row in monday["rows"][:3]:
            row.update(lastPrice=70, bidPrice=69.95, askPrice=70.05)
        self.archive([monday])
        result = evaluate_archive(self.directory)
        self.assertFalse(result["train"]["riskReviewRequired"])
        self.assertFalse(result["holdout"]["riskReviewRequired"])
        self.assertTrue(result["continuousRisk"]["riskReviewRequired"])
        self.assertEqual(result["continuousRisk"]["riskReviewAt"], monday["at"])
        self.assertIn("PORTFOLIO_DRAWDOWN_REVIEW_REQUIRED", result["reasons"])


if __name__ == "__main__":
    unittest.main()
