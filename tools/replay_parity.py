"""Bounded, read-only comparison of PaperEngine with the research replay.

The live engine rejects old wall-clock timestamps. For historical comparison we
freeze its clock at each recorded scan time; this does not alter either engine.
Only verified, non-synthetic OpenD records are accepted by the archive loader.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import paperdesk.engine as engine_module
from paperdesk.engine import PaperEngine
from quantlab import intraday_eval as research


MAX_SCANS = 16
EQUITY_TOLERANCE_DOLLARS = 0.10
PRICE_TOLERANCE_DOLLARS = 0.0001
FEE_TOLERANCE_DOLLARS = 0.011


def _frozen_datetime(at):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return at.astimezone(tz) if tz else at.replace(tzinfo=None)

    return FrozenDateTime


def _trade_differences(paper, offline):
    issues = []
    if len(paper) != len(offline):
        issues.append({"field": "tradeCount", "paper": len(paper), "offline": len(offline)})
    for index, (p, o) in enumerate(zip(paper, offline)):
        for paper_key, offline_key in (("code", "code"), ("side", "side"),
                                       ("qty", "qty"), ("at", "fillAt"),
                                       ("signalAt", "signalAt")):
            if p.get(paper_key) != o.get(offline_key):
                issues.append({"field": f"trade[{index}].{paper_key}",
                               "paper": p.get(paper_key), "offline": o.get(offline_key)})
        for paper_key, offline_key, tolerance in (("price", "fillPrice", PRICE_TOLERANCE_DOLLARS),
                                                   ("fee", "fee", FEE_TOLERANCE_DOLLARS)):
            if abs(p[paper_key] - o[offline_key]) > tolerance:
                issues.append({"field": f"trade[{index}].{paper_key}",
                               "paper": p[paper_key], "offline": o[offline_key]})
    return issues


def compare_scans(scans: list[dict]) -> dict:
    """Compare each prefix's trades and equity; any discrepancy is NO_GO_PARITY."""
    if not scans or len(scans) > MAX_SCANS:
        raise ValueError(f"provide 1 to {MAX_SCANS} scans")
    if any(scan.get("synthetic") is not False or
           scan.get("source") != "LOCAL_MOOMOO_OPEND" for scan in scans):
        raise ValueError("parity comparison requires non-synthetic OpenD scans")
    prepared = [dict(scan, _at=research._time(scan["at"])) for scan in scans]
    prepared.sort(key=lambda item: item["_at"])
    if any(a["_at"] >= b["_at"] for a, b in zip(prepared, prepared[1:])):
        raise ValueError("duplicate or nonincreasing scan timestamps")
    rounds = []
    with tempfile.TemporaryDirectory(prefix="paper-parity-") as temporary:
        engine = PaperEngine(Path(temporary) / "paper.json", demo=False)
        engine.control("start")
        for index, scan in enumerate(prepared):
            with patch.object(engine_module, "datetime", _frozen_datetime(scan["_at"])):
                engine.apply(scan)
            offline = research._segment(prepared[:index + 1])
            paper_equity = round(engine._equity(), 2)
            offline_equity = offline["equityCurve"][-1]["equity"]
            issues = _trade_differences(engine.account["trades"], offline["trades"])
            equity_difference = round(paper_equity - offline_equity, 2)
            if abs(equity_difference) > EQUITY_TOLERANCE_DOLLARS:
                issues.append({"field": "equity", "paper": paper_equity,
                               "offline": offline_equity,
                               "differenceDollars": equity_difference})
            offline_usable = research._scan_ok(scan)
            paper_usable = engine.gate not in {
                "INVALID_OR_STALE_SCAN_TIME", "DUPLICATE_OR_OUT_OF_ORDER_SCAN",
                "INVALID_MARKET_SCAN", "OUTSIDE_REGULAR_SESSION",
                "INCOMPLETE_OR_STALE_MARKET_SCAN", "BENCHMARK_QUOTES_MISSING_OR_STALE"}
            if offline_usable != paper_usable:
                issues.append({"field": "scanUsable", "paper": paper_usable,
                               "offline": offline_usable})
            paper_review = bool(engine.account.get("riskReviewRequired", False))
            offline_review = bool(offline.get("riskReviewRequired", False))
            if paper_review != offline_review:
                issues.append({"field": "riskReviewRequired", "paper": paper_review,
                               "offline": offline_review})
            paper_review_at = engine.account.get("riskReviewAt") or None
            offline_review_at = offline.get("riskReviewAt") or None
            if paper_review_at != offline_review_at:
                issues.append({"field": "riskReviewAt", "paper": paper_review_at,
                               "offline": offline_review_at})
            paper_peak = round(engine.account.get("peakEquity", 100000), 2)
            offline_peak = offline.get("peakEquity")
            if offline_peak is not None and abs(paper_peak - offline_peak) > EQUITY_TOLERANCE_DOLLARS:
                issues.append({"field": "peakEquity", "paper": paper_peak,
                               "offline": offline_peak})
            rounds.append({"at": scan["at"], "paperGate": engine.gate,
                           "paperRunning": engine.running,
                           "paperRiskReviewRequired": paper_review,
                           "offlineRiskReviewRequired": offline_review,
                           "paperRiskReviewAt": paper_review_at,
                           "offlineRiskReviewAt": offline_review_at,
                           "paperPeakEquityDollars": paper_peak,
                           "offlinePeakEquityDollars": offline_peak,
                           "paperPending": len(engine.account["pending"]),
                           "paperPositions": len(engine.account["positions"]),
                           "offlinePositions": offline["openPositions"],
                           "paperTrades": len(engine.account["trades"]),
                           "offlineTrades": len(offline["trades"]),
                           "paperEquityDollars": paper_equity,
                           "offlineEquityDollars": offline_equity,
                           "equityDifferenceDollars": equity_difference,
                           "paperDayStartEquityDollars": round(engine.account["dayStartEquity"], 2),
                           "paperFeesDollars": round(sum(t["fee"] for t in engine.account["trades"]), 4),
                           "offlineFeesDollars": round(sum(t["fee"] for t in offline["trades"]), 4),
                           "issues": issues})
            if len(engine.account["positions"]) != offline["openPositions"]:
                rounds[-1]["issues"].append({"field": "openPositions",
                                             "paper": len(engine.account["positions"]),
                                             "offline": offline["openPositions"]})
    issue_count = sum(len(item["issues"]) for item in rounds)
    return {"status": "NO_GO_PARITY" if issue_count else "PARITY_CHECKED",
            "scanCount": len(rounds), "issueCount": issue_count, "rounds": rounds,
            "scope": "trade sequence, scan acceptance, position count and marked equity; "
                     "historical clock frozen per scan; not a profit validation"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="OpenD scan archive directory or one record")
    parser.add_argument("--max-scans", type=int, default=MAX_SCANS,
                        help=f"compare only earliest records, 1–{MAX_SCANS} (default {MAX_SCANS})")
    args = parser.parse_args(argv)
    if not 1 <= args.max_scans <= MAX_SCANS:
        parser.error(f"--max-scans must be 1–{MAX_SCANS}")
    files = research._record_files(args.archive)
    scans = [research._read_scan(file) for file in files[:args.max_scans]]
    result = compare_scans(scans)
    result["archiveRecordsAvailable"] = len(files)
    result["selection"] = "first records in archive filename order"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["status"] == "NO_GO_PARITY" else 0


if __name__ == "__main__":
    raise SystemExit(main())
