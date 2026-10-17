"""Conservative, offline evaluation of recorded OpenD market scans.

This is a research estimate from quote snapshots, not a fill simulator or
evidence of future profit. Signals at t can first fill at t+1. The strategy and
quality gates are fixed here to make repeated evaluations comparable.
"""

from __future__ import annotations

import argparse
from datetime import datetime, time as clock_time
import hashlib
import json
import math
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
INITIAL_CASH = 100_000.0
SIDE_SLIPPAGE_BPS = 10.0
SIDE_FEE_BPS = 2.0
MAX_EXECUTION_GAP_SECONDS = 300
MIN_REGULAR_DAYS = 60
MIN_CLOSED_ROUND_TRIPS = 100
MIN_HOLDOUT_ROUND_TRIPS = 30
MIN_COVERAGE = 0.8
MIN_COMPLETE_SCAN_FRACTION = .95
MAX_MEDIAN_INTERVAL_SECONDS = 180
MAX_LARGEST_WITHIN_DAY_GAP_SECONDS = 900


def _number(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _time(value):
    if not isinstance(value, str):
        raise ValueError("snapshot at must be an ISO timestamp")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("snapshot timestamps must include a timezone")
    return stamp


def _record_files(path: Path) -> list[Path]:
    path = Path(path)
    files = sorted([*path.glob("*.json"), *path.glob("*.json.gz")]) if path.is_dir() else [path]
    if not files:
        raise ValueError("no snapshot JSON files found")
    return files


def _read_scan(file: Path) -> dict:
    required = {"source", "at", "universe", "received", "usable", "complete",
                "regular", "rows", "benchmarks", "errors", "synthetic"}
    try:
        if file.name.endswith(".json.gz"):
            from paperdesk.record import read_record
            scan = read_record(file)
        elif file.name.endswith(".json"):
            body = file.read_bytes()
            digest = file.name.removesuffix(".json").rsplit("-", 1)[-1]
            if len(digest) != 64 or hashlib.sha256(body).hexdigest() != digest:
                raise ValueError("snapshot checksum mismatch")
            scan = json.loads(body.decode("utf-8"))
        else:
            raise ValueError("unsupported snapshot extension")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid snapshot file {file.name}: {exc}") from exc
    if not isinstance(scan, dict) or not required.issubset(scan):
        raise ValueError(f"invalid snapshot schema: {file.name}")
    if scan["synthetic"] is not False or scan["source"] != "LOCAL_MOOMOO_OPEND":
        raise ValueError(f"synthetic or unverified source rejected: {file.name}")
    if not isinstance(scan["rows"], list) or not isinstance(scan["benchmarks"], dict):
        raise ValueError(f"invalid rows or benchmarks: {file.name}")
    scan["_at"] = _time(scan["at"])
    scan["_fileDigest"] = file.name.removesuffix(".json.gz").removesuffix(".json").rsplit("-", 1)[-1]
    scan["_fileName"] = file.name
    return scan


def load_snapshots(path: Path) -> list[dict]:
    """Convenience loader for small archives; CLI uses bounded-memory streaming."""
    scans = [_read_scan(file) for file in _record_files(path)]
    scans.sort(key=lambda x: x["_at"])
    if any(a["_at"] >= b["_at"] for a, b in zip(scans, scans[1:])):
        raise ValueError("duplicate or nonincreasing snapshot timestamps")
    return scans


def _quote_ok(row, at):
    if not isinstance(row, dict) or not row.get("usable") or not row.get("fresh"):
        return False
    price = _number(row.get("lastPrice"))
    if price is None or price <= 0:
        return False
    try:
        source = _time(row["sourceAsOf"])
    except (KeyError, TypeError, ValueError):
        return False
    age = (at - source).total_seconds()
    return 0 <= age <= 180 and source.astimezone(ET).date() == at.astimezone(ET).date()


def _bbo(row):
    if not row or not row.get("bboValid"):
        return None
    bid, ask = _number(row.get("bidPrice")), _number(row.get("askPrice"))
    if bid is None or ask is None or not 0 < bid < ask:
        return None
    return bid, ask


def _quote_after(row, signal_at):
    try:
        return _time(row["sourceAsOf"]) > signal_at
    except (KeyError, TypeError, ValueError):
        return False


def _scan_ok(scan):
    if not scan["regular"] or not scan["complete"] or scan["errors"]:
        return False
    universe, received, usable = (scan[k] for k in ("universe", "received", "usable"))
    if any(not isinstance(v, int) or isinstance(v, bool) for v in (universe, received, usable)):
        return False
    if universe < 2000 or not (0 <= usable <= received == universe):
        return False
    if usable / universe < MIN_COVERAGE:
        return False
    codes = [r.get("code") for r in scan["rows"] if isinstance(r, dict)]
    if len(codes) != universe or any(not isinstance(code, str) for code in codes) or len(set(codes)) != universe:
        return False
    if sum(_quote_ok(r, scan["_at"]) for r in scan["rows"]) != usable:
        return False
    bench = scan["benchmarks"]
    return all(_quote_ok(bench.get(symbol), scan["_at"]) for symbol in ("SPY", "QQQ"))


def _rows(scan):
    return {r["code"]: r for r in scan["rows"] if isinstance(r, dict) and
            isinstance(r.get("code"), str) and _quote_ok(r, scan["_at"])}


def _signals(scan, rows, positions, cooldowns):
    spy, qqq = (scan["benchmarks"][x] for x in ("SPY", "QQQ"))
    bench_up = all((_number(r.get("changePct")) or 0) > 0.2 for r in (spy, qqq))
    breadth = sum((_number(r.get("changePct")) or 0) > 0 for r in rows.values()) / len(rows) if rows else 0
    bull = bench_up and breadth > 0.52
    picks = []
    if bull:
        for code, row in rows.items():
            price, change, turnover, spread = (_number(row.get(k)) for k in
                                               ("lastPrice", "changePct", "turnover", "spreadBps"))
            if (None in (price, change, turnover, spread) or not 5 <= price <= 500 or
                    not 1 <= change <= 8 or turnover < 5_000_000 or spread > 30 or
                    code in positions or code in cooldowns):
                continue
            score = change * 10 + min(turnover / 10_000_000, 20) - spread / 10
            picks.append((-score, code))
        picks.sort()
    exits = []
    for code, p in positions.items():
        row = rows.get(code)
        if row is None:
            continue
        price = _number(row["lastPrice"])
        peak = max(p["peak"], price)
        p["peak"] = peak
        age = (scan["_at"] - p["opened"]).total_seconds()
        if (price <= p["entry"] * .97 or price <= peak * .98 or
                price >= p["entry"] * 1.05 or age >= 172800 or not bull):
            exits.append(code)
    return exits, [code for _, code in picks[:12]], bull


def _segment(scans, *, slippage_bps=SIDE_SLIPPAGE_BPS, fee_bps=SIDE_FEE_BPS):
    cash, positions, cooldowns = INITIAL_CASH, {}, set()
    pending = {"sells": [], "buys": []}
    trades, curve, closed, missed, stale_held_marks = [], [], [], 0, 0
    last_prices = {}
    previous = None
    first_spy = last_spy = None
    current_day = None
    day_start_equity = INITIAL_CASH
    day_halted = False
    scan_count = 0

    def equity():
        # Include an adverse liquidation estimate for open positions.
        return cash + sum(p["qty"] * last_prices.get(code, p["entry"]) *
                          (1 - (slippage_bps + fee_bps) / 10_000)
                          for code, p in positions.items())

    for scan in scans:
        scan_count += 1
        at = scan["_at"]
        day = at.astimezone(ET).date()
        if day != current_day:
            cooldowns.clear()
            current_day = day
            day_start_equity = equity()
            day_halted = False
        good = _scan_ok(scan)
        rows = _rows(scan) if good else {}
        next_round = previous is not None and 0 < (at - previous).total_seconds() <= MAX_EXECUTION_GAP_SECONDS
        if good:
            spy_bbo = _bbo(scan["benchmarks"]["SPY"])
            if (spy_bbo and first_spy is None and next_round and
                    _quote_after(scan["benchmarks"]["SPY"], previous)):
                first_spy = spy_bbo[1]
            if spy_bbo and first_spy is not None:
                last_spy = spy_bbo[0]
        for code in positions:
            bbo = _bbo(rows.get(code))
            if bbo:
                last_prices[code] = bbo[0]
            else:
                stale_held_marks += 1
        if equity() < day_start_equity * .98:
            day_halted = True
        if good and next_round:
            for code in pending["sells"]:
                row = rows.get(code)
                bbo = _bbo(row)
                if code not in positions or bbo is None or not _quote_after(row, positions[code]["exit_signal"]):
                    missed += 1
                    continue
                p = positions.pop(code)
                price = bbo[0] * (1 - slippage_bps / 10_000)
                fee = p["qty"] * price * fee_bps / 10_000
                cash += p["qty"] * price - fee
                closed.append((cash, code))
                cooldowns.add(code)
                trades.append({"signalAt": p["exit_signal"].isoformat(), "fillAt": at.isoformat(),
                               "code": code, "side": "SELL", "qty": p["qty"],
                               "fillPrice": round(price, 6), "fee": round(fee, 4)})
            for code in pending["buys"]:
                if len(positions) >= 3:
                    break
                row = rows.get(code)
                bbo = _bbo(row)
                if (bbo is None or code in positions or code in cooldowns or
                        not _quote_after(row, previous)):
                    missed += 1
                    continue
                if equity() < day_start_equity * .98:
                    day_halted = True
                if day_halted:
                    missed += 1
                    continue
                price = bbo[1] * (1 + slippage_bps / 10_000)
                fee_rate = fee_bps / 10_000
                budget = min(equity() * .15, cash * .95)
                qty = math.floor(budget / (price * (1 + fee_rate)))
                if qty <= 0:
                    continue
                fee = qty * price * fee_rate
                cash -= qty * price + fee
                positions[code] = {"entry": price, "peak": _number(row["lastPrice"]),
                                   "qty": qty, "opened": at, "exit_signal": None}
                last_prices[code] = bbo[0]
                trades.append({"signalAt": previous.isoformat(), "fillAt": at.isoformat(),
                               "code": code, "side": "BUY", "qty": qty,
                               "fillPrice": round(price, 6), "fee": round(fee, 4)})
        elif pending["sells"] or pending["buys"]:
            missed += len(pending["sells"]) + len(pending["buys"])
        pending = {"sells": [], "buys": []}
        if equity() < day_start_equity * .98:
            day_halted = True
        if good:
            exits, picks, bull = _signals(scan, rows, positions, cooldowns)
            for code in exits:
                positions[code]["exit_signal"] = at
            pending["sells"] = exits
            if bull and not day_halted:
                pending["buys"] = [code for code in picks if code not in positions][:3]
        curve.append({"at": at.isoformat(), "equity": round(equity(), 2), "scanUsable": good})
        previous = at
    peak, drawdown = INITIAL_CASH, 0.0
    for point in curve:
        peak = max(peak, point["equity"])
        drawdown = min(drawdown, point["equity"] / peak - 1)
    net_return = equity() / INITIAL_CASH - 1
    spy_return = None
    if first_spy is not None and last_spy is not None and first_spy > 0:
        gross_spy = last_spy / first_spy
        spy_return = gross_spy * (1 - slippage_bps / 10_000 - fee_bps / 10_000) / \
                     (1 + slippage_bps / 10_000 + fee_bps / 10_000) - 1
    return {"snapshots": scan_count, "netReturnPct": round(net_return * 100, 4),
            "maxDrawdownPct": round(drawdown * 100, 4),
            "spyNetReturnPct": round(spy_return * 100, 4) if spy_return is not None else None,
            "completedRoundTrips": len(closed), "openPositions": len(positions),
            "missedOrCancelledFills": missed, "staleHeldMarks": stale_held_marks,
            "trades": trades, "equityCurve": curve}


def _split_index(regular):
    all_days = sorted({s["_at"].astimezone(ET).date() for s in regular})
    if len(all_days) >= 2:
        split_day = all_days[max(1, min(len(all_days) - 1, int(len(all_days) * .7)))]
        return next(i for i, s in enumerate(regular) if s["_at"].astimezone(ET).date() >= split_day)
    return max(1, min(len(regular) - 1, int(len(regular) * .7))) if len(regular) > 1 else len(regular)


def _report(scans, train, holdout, holdout_stress, split):
    if not scans:
        return {"status": "NO_GO", "reasons": ["NO_SNAPSHOTS"]}
    regular = [s for s in scans if s["regular"]]
    if not regular:
        return {"status": "NO_GO", "reasons": ["NO_REGULAR_SESSION_SCANS"],
                "sample": {"snapshots": len(scans), "regularSnapshots": 0,
                           "archiveSha256List": [{"file": s.get("_fileName"),
                                                  "sha256": s.get("_fileDigest")} for s in scans]}}
    good = [s["_good"] if "_good" in s else _scan_ok(s) for s in regular]
    days = {s["_at"].astimezone(ET).date() for s in regular}
    good_by_day = {}
    for scan, usable in zip(regular, good):
        if usable:
            good_by_day.setdefault(scan["_at"].astimezone(ET).date(), []).append(scan["_at"].astimezone(ET))
    full_session_days = sum(bool(stamps and min(stamps).time() <= clock_time(9, 45) and
                                 max(stamps).time() >= clock_time(15, 45))
                            for stamps in good_by_day.values())
    gaps = [(b["_at"] - a["_at"]).total_seconds() for a, b in zip(regular, regular[1:])
            if a["_at"].astimezone(ET).date() == b["_at"].astimezone(ET).date()]
    median_gap = median(gaps) if gaps else None
    reasons = []
    if len(days) < MIN_REGULAR_DAYS:
        reasons.append("FEWER_THAN_60_REGULAR_TRADING_DAYS")
    if full_session_days / len(days) < MIN_COMPLETE_SCAN_FRACTION:
        reasons.append("FEWER_THAN_95_PERCENT_FULL_SESSION_DAYS")
    holdout_days = {s["_at"].astimezone(ET).date() for s in regular[split:]}
    if len(holdout_days) < 20:
        reasons.append("FEWER_THAN_20_HOLDOUT_DAYS")
    if train["completedRoundTrips"] + holdout["completedRoundTrips"] < MIN_CLOSED_ROUND_TRIPS:
        reasons.append("FEWER_THAN_100_COMPLETED_ROUND_TRIPS")
    if holdout["completedRoundTrips"] < MIN_HOLDOUT_ROUND_TRIPS:
        reasons.append("FEWER_THAN_30_HOLDOUT_ROUND_TRIPS")
    if holdout["netReturnPct"] <= 0:
        reasons.append("HOLDOUT_NOT_PROFITABLE_AFTER_COSTS")
    if holdout["spyNetReturnPct"] is None or holdout["netReturnPct"] <= holdout["spyNetReturnPct"]:
        reasons.append("HOLDOUT_NOT_ABOVE_SPY_AFTER_COSTS")
    if holdout_stress["netReturnPct"] <= 0:
        reasons.append("HOLDOUT_NOT_PROFITABLE_AT_DOUBLE_COSTS")
    if sum(good) / len(regular) < MIN_COMPLETE_SCAN_FRACTION:
        reasons.append("COMPLETE_FRESH_SCAN_FRACTION_BELOW_95_PERCENT")
    if train["staleHeldMarks"] or holdout["staleHeldMarks"]:
        reasons.append("OPEN_POSITION_MARKS_MISSING_VALID_BID")
    if median_gap is None or median_gap > MAX_MEDIAN_INTERVAL_SECONDS or any(g > MAX_LARGEST_WITHIN_DAY_GAP_SECONDS for g in gaps):
        reasons.append("INSUFFICIENT_INTRADAY_FREQUENCY_OR_GAPS")
    if len(regular) < 2 or not any(good[:split]) or not any(good[split:]):
        reasons.append("TRAIN_OR_HOLDOUT_HAS_NO_VALID_SCANS")
    return {"status": "NO_GO" if reasons else "RESEARCH_ONLY", "reasons": reasons,
            "strategy": "Fixed PaperDesk long-only momentum screen; signal at t, earliest fill at t+1",
            "codeProvenance": {
                "evaluatorSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "paperEngineSha256": hashlib.sha256((Path(__file__).resolve().parents[1] /
                                                        "paperdesk" / "engine.py").read_bytes()).hexdigest()},
            "fixedRules": {"marketDirection": "SPY and QQQ each > +0.2%, breadth > 52%",
                           "selection": "price $5-$500, gain 1%-8%, turnover >= $5m, spread <= 30 bps; rank by fixed score",
                           "maxPositions": 3, "positionBudgetFraction": .15,
                           "stopLossPct": 3, "trailingStopPct": 2, "takeProfitPct": 5,
                           "maxHoldDays": 2, "dailyLossHaltPct": 2},
            "costs": {"slippageBpsPerSide": SIDE_SLIPPAGE_BPS, "feeBpsPerSide": SIDE_FEE_BPS,
                      "stressSlippageBpsPerSide": 2 * SIDE_SLIPPAGE_BPS,
                      "stressFeeBpsPerSide": 2 * SIDE_FEE_BPS,
                      "fillProxy": "next complete scan ask for buys, bid for sells; no execution guarantee"},
            "sample": {"snapshots": len(scans), "regularSnapshots": len(regular),
                       "excludedNonRegularScans": len(scans) - len(regular),
                       "regularTradingDays": len(days),
                       "fullSessionDays": full_session_days,
                       "completeFreshScanCount": sum(good),
                       "completeFreshScanFraction": round(sum(good) / len(regular), 4),
                       "medianWithinDayGapSeconds": median_gap,
                       "largestWithinDayGapSeconds": max(gaps) if gaps else None,
                       "archiveDigestSha256": hashlib.sha256("\n".join(
                           s.get("_fileDigest", "") for s in scans).encode("ascii")).hexdigest(),
                       "archiveSha256List": [{"file": s.get("_fileName"), "sha256": s.get("_fileDigest")}
                                             for s in scans],
                       "firstAt": scans[0]["at"], "lastAt": scans[-1]["at"],
                       "trainEnd": regular[split - 1]["at"],
                       "holdoutStart": regular[split]["at"] if split < len(regular) else None,
                       "holdoutTradingDays": len(holdout_days),
                       "scope": "Recorded current-universe scans only; point-in-time valid for capture days"},
            "train": train, "holdout": holdout, "holdoutStress": holdout_stress,
            "limitations": ["90-second snapshots cannot validate second-scale trades or actual fills",
                            "Archive checksum proves file integrity, not broker origin; source label is self-reported",
                            "No future profitability claim; selection and survivorship need independent audit"]}


def evaluate(scans: list[dict]) -> dict:
    """Convenience evaluator for small in-memory archives."""
    if not scans:
        return {"status": "NO_GO", "reasons": ["NO_SNAPSHOTS"]}
    scans = sorted(scans, key=lambda x: x["_at"])
    if any(a["_at"] >= b["_at"] for a, b in zip(scans, scans[1:])):
        raise ValueError("duplicate snapshot timestamps")
    if any(s.get("synthetic") is not False or s.get("source") != "LOCAL_MOOMOO_OPEND" for s in scans):
        raise ValueError("only actual LOCAL_MOOMOO_OPEND snapshots are accepted")
    regular = [s for s in scans if s["regular"]]
    if not regular:
        return _report(scans, None, None, None, 0)
    split = _split_index(regular)
    train, holdout = _segment(regular[:split]), _segment(regular[split:])
    stress = _segment(regular[split:], slippage_bps=2 * SIDE_SLIPPAGE_BPS,
                      fee_bps=2 * SIDE_FEE_BPS)
    return _report(scans, train, holdout, stress, split)


def evaluate_archive(path: Path) -> dict:
    """Evaluate large archives using one metadata pass and streaming replays."""
    path = Path(path)
    if not path.exists() or (path.is_dir() and not any(path.glob("*.json.gz"))
                             and not any(path.glob("*.json"))):
        return {"status": "NO_GO", "reasons": ["NO_RECORDED_SCANS"],
                "sample": {"snapshots": 0}}
    metadata = []
    for file in _record_files(path):
        scan = _read_scan(file)
        metadata.append({"at": scan["at"], "_at": scan["_at"],
                         "source": scan["source"], "regular": scan["regular"],
                         "synthetic": scan["synthetic"], "_good": _scan_ok(scan),
                         "_fileDigest": scan["_fileDigest"],
                         "_fileName": scan["_fileName"], "_filePath": file})
    metadata.sort(key=lambda x: x["_at"])
    if any(a["_at"] >= b["_at"] for a, b in zip(metadata, metadata[1:])):
        raise ValueError("duplicate snapshot timestamps")
    regular = [s for s in metadata if s["regular"]]
    if not regular:
        return _report(metadata, None, None, None, 0)
    split = _split_index(regular)

    def replay(items):
        for item in items:
            yield _read_scan(item["_filePath"])

    train = _segment(replay(regular[:split]))
    holdout = _segment(replay(regular[split:]))
    stress = _segment(replay(regular[split:]), slippage_bps=2 * SIDE_SLIPPAGE_BPS,
                      fee_bps=2 * SIDE_FEE_BPS)
    return _report(metadata, train, holdout, stress, split)


def main():
    parser = argparse.ArgumentParser(description="Evaluate recorded real OpenD scans, JSON output")
    parser.add_argument("path", type=Path, help="scan JSON file or scans directory")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    try:
        result = evaluate_archive(args.path)
    except (ValueError, OSError) as exc:
        result = {"status": "NO_GO", "reasons": ["INVALID_OR_UNVERIFIED_INPUT"], "error": str(exc)}
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
    print(payload, end="")


if __name__ == "__main__":
    main()
