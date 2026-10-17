"""Conservative, offline evaluation of recorded OpenD market scans.

This is a research estimate from quote snapshots, not a fill simulator or
evidence of future profit. Signals at t can first fill at t+1. The strategy and
quality gates are fixed here to make repeated evaluations comparable.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time as clock_time, timezone
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
REGISTRATION_SCHEMA = 1


def _code_provenance():
    return {"evaluatorSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "paperEngineSha256": hashlib.sha256((Path(__file__).resolve().parents[1] /
                                                     "paperdesk" / "engine.py").read_bytes()).hexdigest()}


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


def _market_bull(scan, rows):
    spy, qqq = (scan["benchmarks"][x] for x in ("SPY", "QQQ"))
    bench_up = all((_number(r.get("changePct")) or 0) > 0.2 for r in (spy, qqq))
    breadth = sum((_number(r.get("changePct")) or 0) > 0 for r in rows.values()) / len(rows) if rows else 0
    return bench_up and breadth > 0.52


def _ranked_picks(rows):
    """Match PaperEngine's complete ranked candidate list using the actual BBO."""
    picks = []
    for code, row in rows.items():
        price, change, turnover = (_number(row.get(k)) for k in
                                   ("lastPrice", "changePct", "turnover"))
        bbo = _bbo(row)
        if price is None or change is None or turnover is None or bbo is None:
            continue
        bid, ask = bbo
        spread = (ask - bid) / ((ask + bid) / 2) * 10_000
        if (not 5 <= price <= 500 or not 1 <= change <= 8 or
                turnover < 5_000_000 or spread > 30 or
                not bid * .98 <= price <= ask * 1.02):
            continue
        score = round(change * 10 + min(turnover / 10_000_000, 20) - spread / 10, 2)
        picks.append((-score, code))
    picks.sort()
    return [code for _, code in picks]


def _signals(scan, rows, positions, cooldowns):
    bull = _market_bull(scan, rows)
    picks = _ranked_picks(rows) if bull else []
    exits = []
    for code, p in positions.items():
        row = rows.get(code)
        if row is None:
            continue
        price = _number(row["lastPrice"])
        peak = max(p["peak"], price)
        p["peak"] = peak
        age = (scan["_at"] - p["opened"]).total_seconds()
        bbo = _bbo(row)
        exit_price = min(price, bbo[0]) if bbo else None
        if exit_price is not None and (
                exit_price <= p["entry"] * .97 or exit_price <= peak * .98 or
                exit_price >= p["entry"] * 1.05 or age >= 172800 or not bull):
            exits.append(code)
    return exits, [code for code in picks if code not in positions and code not in cooldowns], bull


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
    peak_equity = INITIAL_CASH
    review_at = None
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
        # PaperEngine still marks held positions from individually valid quotes
        # when the full scan is rejected; that scan cannot execute or signal.
        rows = _rows(scan)
        current_bull = _market_bull(scan, rows) if good else False
        current_candidates = set(_ranked_picks(rows)) if current_bull else set()
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
        peak_equity = max(peak_equity, equity())
        if review_at is None and equity() <= peak_equity * .9:
            review_at = at.isoformat()
        if equity() < day_start_equity * .98:
            day_halted = True
        if good and next_round and review_at is None:
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
                if (bbo is None or not current_bull or code not in current_candidates or
                        code in positions or code in cooldowns or
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
        peak_equity = max(peak_equity, equity())
        if review_at is None and equity() <= peak_equity * .9:
            review_at = at.isoformat()
        if equity() < day_start_equity * .98:
            day_halted = True
        if good and review_at is None:
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
            "peakEquity": round(peak_equity, 2), "riskReviewRequired": review_at is not None,
            "riskReviewAt": review_at,
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


def _manifest(scans):
    return [{"file": scan.get("_fileName"), "sha256": scan.get("_fileDigest")}
            for scan in scans]


def _archive_metadata(path):
    metadata = []
    for file in _record_files(path):
        scan = _read_scan(file)
        metadata.append({"at": scan["at"], "_at": scan["_at"],
                         "source": scan["source"], "regular": scan["regular"],
                         "synthetic": scan["synthetic"], "_good": _scan_ok(scan),
                         "_fileDigest": scan["_fileDigest"],
                         "_fileName": scan["_fileName"], "_filePath": file})
    metadata.sort(key=lambda item: item["_at"])
    if any(a["_at"] >= b["_at"] for a, b in zip(metadata, metadata[1:])):
        raise ValueError("duplicate snapshot timestamps")
    return metadata


def _cutoff(value):
    if not isinstance(value, str):
        raise ValueError("cutoffEtDate must be YYYY-MM-DD")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("cutoffEtDate must be YYYY-MM-DD")
    return parsed


def _registration_path(path):
    path = Path(path)
    return (path if path.is_dir() else path.parent) / "validation.lock"


def register_archive(path: Path, cutoff_et_date: str, registration_path: Path | None = None) -> dict:
    """Freeze training files and strategy code before the first holdout scan."""
    cutoff = _cutoff(cutoff_et_date)
    created = datetime.now(timezone.utc)
    if created >= datetime.combine(cutoff, clock_time(9, 30), tzinfo=ET):
        raise ValueError("registration must precede the holdout session open")
    scans = _archive_metadata(path)
    if not any(s["regular"] for s in scans):
        raise ValueError("registration requires regular-session training scans")
    if any(s["_at"].astimezone(ET).date() >= cutoff for s in scans):
        raise ValueError("registration cutoff must follow every existing scan's ET date")
    registration = {"schemaVersion": REGISTRATION_SCHEMA,
                    "createdAtUtc": created.isoformat(),
                    "cutoffEtDate": cutoff.isoformat(),
                    "strategyCode": _code_provenance(),
                    "trainManifest": _manifest(scans),
                    "trainAttemptManifest": _attempt_manifest(path, cutoff)}
    target = Path(registration_path) if registration_path is not None else _registration_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Never silently replace a registration after the holdout has been observed.
    with target.open("x", encoding="utf-8") as output:
        json.dump(registration, output, ensure_ascii=False, indent=2)
        output.write("\n")
    return registration


def _registration_decision(scans, registration, *, registration_path=None,
                           registration_sha256=None, archive_path=None):
    info = {"status": "MISSING", "reason": "MISSING_VALIDATION_REGISTRATION",
            "splitMethod": "EXPLORATORY_70_30", "path": str(registration_path) if registration_path else None,
            "sha256": registration_sha256, "cutoffEtDate": None}
    regular = [s for s in scans if s["regular"]]
    exploratory = _split_index(regular) if regular else 0
    if registration is None:
        return exploratory, info
    info["status"] = "INVALID"
    info["reason"] = "INVALID_VALIDATION_REGISTRATION"
    if not isinstance(registration, dict) or registration.get("schemaVersion") != REGISTRATION_SCHEMA:
        return exploratory, info
    try:
        cutoff = _cutoff(registration.get("cutoffEtDate"))
    except (TypeError, ValueError):
        return exploratory, info
    info["cutoffEtDate"] = cutoff.isoformat()
    info["createdAtUtc"] = registration.get("createdAtUtc")
    info["schemaVersion"] = registration["schemaVersion"]
    try:
        created = _event_time(registration["createdAtUtc"])
    except (KeyError, TypeError, ValueError):
        return exploratory, info
    if created >= datetime.combine(cutoff, clock_time(9, 30), tzinfo=ET):
        info["reason"] = "REGISTRATION_CREATED_AFTER_HOLDOUT_OPEN"
        return exploratory, info
    manifest = registration.get("trainManifest")
    if not isinstance(manifest, list) or not manifest or any(
            not isinstance(item, dict) or set(item) != {"file", "sha256"} or
            not isinstance(item["file"], str) or not isinstance(item["sha256"], str)
            for item in manifest):
        return exploratory, info
    info["trainSnapshotCount"] = len(manifest)
    info["strategyCode"] = registration.get("strategyCode")
    if registration.get("strategyCode") != _code_provenance():
        info["reason"] = "REGISTRATION_STRATEGY_CODE_MISMATCH"
        return exploratory, info
    registered_train = [s for s in scans if s["_at"].astimezone(ET).date() < cutoff]
    if _manifest(registered_train) != manifest or not any(s["regular"] for s in registered_train):
        info["reason"] = "REGISTRATION_TRAIN_ARCHIVE_MISMATCH"
        return exploratory, info
    attempt_manifest = registration.get("trainAttemptManifest")
    if not isinstance(attempt_manifest, list):
        return exploratory, info
    info["trainAttemptEventCount"] = len(attempt_manifest)
    if archive_path is None:
        info["reason"] = "REGISTRATION_TRAIN_ATTEMPT_LOG_UNVERIFIED"
        info["splitMethod"] = "REGISTERED_ET_DATE_UNVERIFIED"
        return sum(s["_at"].astimezone(ET).date() < cutoff for s in regular), info
    try:
        if _attempt_manifest(archive_path, cutoff) != attempt_manifest:
            info["reason"] = "REGISTRATION_TRAIN_ATTEMPT_LOG_MISMATCH"
            return exploratory, info
    except (OSError, EOFError, ValueError):
        info["reason"] = "REGISTRATION_TRAIN_ATTEMPT_LOG_MISMATCH"
        return exploratory, info
    info["status"] = "VALID"
    info["reason"] = None
    info["splitMethod"] = "REGISTERED_ET_DATE"
    return sum(s["_at"].astimezone(ET).date() < cutoff for s in regular), info


def _regular_clock(stamp):
    local = stamp.astimezone(ET)
    return local.weekday() < 5 and clock_time(9, 30) <= local.time() < clock_time(16)


def _event_time(value):
    stamp = _time(value)
    if stamp.utcoffset().total_seconds() != 0:
        raise ValueError("attempt event time must be UTC")
    return stamp


def _read_attempt_event(file):
    from paperdesk.record import read_capture_event
    event = read_capture_event(file)
    started, recorded = _event_time(event["startedAt"]), _event_time(event["recordedAt"])
    if recorded < started:
        raise ValueError("attempt recorded before start")
    return event


def _attempt_manifest(path, cutoff):
    directory = Path(path) / "attempts" if Path(path).is_dir() else Path(path).parent / "attempts"
    manifest = []
    for file in sorted(directory.glob("*.json.gz")) if directory.is_dir() else []:
        event = _read_attempt_event(file)
        if _event_time(event["startedAt"]).astimezone(ET).date() < cutoff:
            manifest.append({"file": file.name, "sha256": hashlib.sha256(file.read_bytes()).hexdigest()})
    return manifest


def _attempt_decision(scans, path):
    info = {"status": "MISSING", "reasons": ["MISSING_CAPTURE_ATTEMPT_LOG"],
            "totalAttempts": None, "failedAttempts": None,
            "regularAttempts": None, "regularFailedAttempts": None,
            "regularCompleteScanFraction": None, "engineErrors": None}
    if path is None:
        return info
    directory = Path(path) / "attempts" if Path(path).is_dir() else Path(path).parent / "attempts"
    files = sorted(directory.glob("*.json.gz")) if directory.is_dir() else []
    if not files:
        return info
    info["status"] = "INVALID"
    info["reasons"] = []
    attempts = {}
    try:
        for file in files:
            event = _read_attempt_event(file)
            attempts.setdefault(event["attemptId"], []).append(event)
    except (OSError, EOFError, ValueError, KeyError, TypeError):
        info["reasons"].append("INVALID_CAPTURE_ATTEMPT_LOG")
        return info
    scan_by_file = {s.get("_fileName"): s for s in scans}
    success_files = []
    regular_attempts = regular_failed = failed = engine_errors = 0
    for events in attempts.values():
        phases = [event["phase"] for event in events]
        starts = [event for event in events if event["phase"] == "STARTED"]
        archives = [event for event in events if event["phase"] == "SCAN_ARCHIVED"]
        failures = [event for event in events if event["phase"] in ("FEED_ERROR", "RECORD_ERROR")]
        engine = [event for event in events if event["phase"] == "ENGINE_ERROR"]
        if (len(starts) != 1 or len(archives) + len(failures) != 1 or len(engine) > 1 or
                (engine and not archives) or len(phases) != len(set(phases)) or
                any(event["startedAt"] != starts[0]["startedAt"] for event in events)):
            info["reasons"].append("INCOMPLETE_OR_CONFLICTING_CAPTURE_ATTEMPT_LOG")
            continue
        terminal = (archives or failures)[0]
        if (_event_time(terminal["recordedAt"]) < _event_time(starts[0]["recordedAt"]) or
                (engine and _event_time(engine[0]["recordedAt"]) <
                 _event_time(terminal["recordedAt"]))):
            info["reasons"].append("INCOMPLETE_OR_CONFLICTING_CAPTURE_ATTEMPT_LOG")
            continue
        if engine:
            engine_errors += 1
        if failures:
            failed += 1
            if _regular_clock(_event_time(starts[0]["startedAt"])):
                regular_attempts += 1
                regular_failed += 1
        else:
            name = archives[0]["scanFile"]
            success_files.append(name)
            if (_regular_clock(_event_time(starts[0]["startedAt"])) or
                    (name in scan_by_file and scan_by_file[name]["regular"])):
                regular_attempts += 1
    if len(success_files) != len(set(success_files)) or set(success_files) != set(scan_by_file):
        info["reasons"].append("CAPTURE_ATTEMPT_SCAN_MISMATCH")
    if engine_errors:
        info["reasons"].append("PAPER_ENGINE_EXECUTION_ERRORS")
    complete = sum(bool(s["_good"] if "_good" in s else _scan_ok(s))
                   for s in scans if s["regular"])
    fraction = min(1.0, complete / regular_attempts) if regular_attempts else 0.0
    if fraction < MIN_COMPLETE_SCAN_FRACTION:
        info["reasons"].append("CAPTURE_ATTEMPT_COVERAGE_BELOW_95_PERCENT")
    info.update(status="VALID" if not info["reasons"] else "INVALID",
                reasons=list(dict.fromkeys(info["reasons"])), totalAttempts=len(attempts),
                failedAttempts=failed, regularAttempts=regular_attempts,
                regularFailedAttempts=regular_failed,
                regularCompleteScanFraction=round(fraction, 4), engineErrors=engine_errors,
                calendarLimit="Regular attempt hours use ET weekdays 09:30-16:00; no holiday/half-day calendar")
    return info


def _report(scans, train, holdout, holdout_stress, full_run, split,
            registration_info, attempts_info):
    if not scans:
        return {"status": "NO_GO", "reasons": ["NO_SNAPSHOTS"]}
    regular = [s for s in scans if s["regular"]]
    if not regular:
        return {"status": "NO_GO", "reasons": ["NO_REGULAR_SESSION_SCANS", registration_info["reason"],
                                                *attempts_info["reasons"]],
                "validationRegistration": registration_info,
                "captureAttempts": attempts_info,
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
    if registration_info["reason"]:
        reasons.append(registration_info["reason"])
    reasons.extend(attempts_info["reasons"])
    if (train["riskReviewRequired"] or holdout["riskReviewRequired"] or
            holdout_stress["riskReviewRequired"] or full_run["riskReviewRequired"]):
        reasons.append("PORTFOLIO_DRAWDOWN_REVIEW_REQUIRED")
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
            "codeProvenance": _code_provenance(),
            "validationRegistration": registration_info,
            "captureAttempts": attempts_info,
            "fixedRules": {"marketDirection": "SPY and QQQ each > +0.2%, breadth > 52%",
                           "selection": "price $5-$500, gain 1%-8%, turnover >= $5m, spread <= 30 bps; rank by fixed score",
                           "maxPositions": 3, "positionBudgetFraction": .15,
                           "stopLossPct": 3, "trailingStopPct": 2, "takeProfitPct": 5,
                           "maxHoldDays": 2, "dailyLossHaltPct": 2,
                           "portfolioPeakDrawdownReviewPct": 10},
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
            "continuousRisk": {key: full_run[key] for key in
                               ("riskReviewRequired", "riskReviewAt", "peakEquity")},
            "limitations": ["90-second snapshots cannot validate second-scale trades or actual fills",
                            "Archive checksum proves file integrity, not broker origin; source label is self-reported",
                            "No future profitability claim; selection and survivorship need independent audit"]}


def evaluate(scans: list[dict], registration: dict | None = None) -> dict:
    """Convenience evaluator for small in-memory archives."""
    if not scans:
        return {"status": "NO_GO", "reasons": ["NO_SNAPSHOTS"]}
    scans = sorted(scans, key=lambda x: x["_at"])
    if any(a["_at"] >= b["_at"] for a, b in zip(scans, scans[1:])):
        raise ValueError("duplicate snapshot timestamps")
    if any(s.get("synthetic") is not False or s.get("source") != "LOCAL_MOOMOO_OPEND" for s in scans):
        raise ValueError("only actual LOCAL_MOOMOO_OPEND snapshots are accepted")
    regular = [s for s in scans if s["regular"]]
    split, registration_info = _registration_decision(scans, registration)
    attempts_info = _attempt_decision(scans, None)
    if not regular:
        return _report(scans, None, None, None, None, 0, registration_info, attempts_info)
    train, holdout = _segment(regular[:split]), _segment(regular[split:])
    stress = _segment(regular[split:], slippage_bps=2 * SIDE_SLIPPAGE_BPS,
                      fee_bps=2 * SIDE_FEE_BPS)
    full_run = _segment(regular)
    return _report(scans, train, holdout, stress, full_run, split,
                   registration_info, attempts_info)


def evaluate_archive(path: Path, registration_path: Path | None = None) -> dict:
    """Evaluate large archives using one metadata pass and streaming replays."""
    path = Path(path)
    if not path.exists() or (path.is_dir() and not any(path.glob("*.json.gz"))
                             and not any(path.glob("*.json"))):
        return {"status": "NO_GO", "reasons": ["NO_RECORDED_SCANS"],
                "sample": {"snapshots": 0}}
    metadata = _archive_metadata(path)
    lock_path = Path(registration_path) if registration_path is not None else _registration_path(path)
    registration = None
    registration_digest = None
    if lock_path.exists():
        body = lock_path.read_bytes()
        registration_digest = hashlib.sha256(body).hexdigest()
        try:
            registration = json.loads(body)
        except (UnicodeError, json.JSONDecodeError):
            registration = {}
    split, registration_info = _registration_decision(
        metadata, registration, registration_path=lock_path,
        registration_sha256=registration_digest, archive_path=path)
    attempts_info = _attempt_decision(metadata, path)
    regular = [s for s in metadata if s["regular"]]
    if not regular:
        return _report(metadata, None, None, None, None, 0, registration_info, attempts_info)

    def replay(items):
        for item in items:
            yield _read_scan(item["_filePath"])

    train = _segment(replay(regular[:split]))
    holdout = _segment(replay(regular[split:]))
    stress = _segment(replay(regular[split:]), slippage_bps=2 * SIDE_SLIPPAGE_BPS,
                      fee_bps=2 * SIDE_FEE_BPS)
    full_run = _segment(replay(regular))
    return _report(metadata, train, holdout, stress, full_run, split,
                   registration_info, attempts_info)


def main():
    parser = argparse.ArgumentParser(description="Evaluate recorded real OpenD scans, JSON output")
    parser.add_argument("path", type=Path, help="scan JSON file or scans directory")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--registration", type=Path, help="registration lock path (default: archive parent/validation.lock)")
    parser.add_argument("--register", action="store_true", help="freeze training archive and strategy before holdout")
    parser.add_argument("--cutoff-et", help="first holdout trading date in America/New_York, YYYY-MM-DD")
    args = parser.parse_args()
    if args.register:
        if not args.cutoff_et or args.out:
            parser.error("--register requires --cutoff-et and does not accept --out")
        try:
            result = register_archive(args.path, args.cutoff_et, args.registration)
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        target = args.registration or _registration_path(args.path)
        print(json.dumps({"registrationPath": str(target),
                          "registrationSha256": hashlib.sha256(Path(target).read_bytes()).hexdigest(),
                          "cutoffEtDate": result["cutoffEtDate"],
                          "trainSnapshots": len(result["trainManifest"]),
                          "trainAttemptEvents": len(result["trainAttemptManifest"])},
                         ensure_ascii=False, indent=2))
        return
    if args.cutoff_et:
        parser.error("--cutoff-et requires --register")
    try:
        result = evaluate_archive(args.path, args.registration)
    except (ValueError, OSError) as exc:
        result = {"status": "NO_GO", "reasons": ["INVALID_OR_UNVERIFIED_INPUT"], "error": str(exc)}
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
    print(payload, end="")


if __name__ == "__main__":
    main()
