"""Export read-only US daily bars from a locally logged-in Moomoo OpenD.

Run on the same Mac as OpenD. This script never calls trade/account APIs.
It deliberately omits historical universe eligibility, which OpenD's current
stock directory cannot establish for earlier dates.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import socket
import tempfile
from datetime import date
from pathlib import Path

FIELDS = ("date", "symbol", "open", "close", "adj_close", "split_factor", "dividend")


def positive(value: object, label: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"invalid {label}: expected positive finite value")
    return number


def nonnegative(value: object) -> float:
    if value is None or value == "":
        return 0.0
    number = float(value)
    if math.isnan(number):
        return 0.0
    if not math.isfinite(number) or number < 0:
        raise ValueError("invalid corporate action value")
    return number


def _records(frame: object) -> list[dict]:
    return frame.to_dict("records")


def _history(client: object, code: str, start: str, end: str, autype: str) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    key = None
    seen_keys = set()
    while True:
        status, frame, next_key = client.request_history_kline(
            code, start=start, end=end, ktype="K_DAY", autype=autype,
            max_count=1000, page_req_key=key, extended_time=False,
        )
        if status != 0:
            raise RuntimeError(f"OpenD historical K-line request failed for {code} ({autype}): {str(frame)[:160]}")
        for row in _records(frame):
            day = str(row["time_key"])[:10]
            date.fromisoformat(day)
            if day in rows:
                raise ValueError(f"duplicate OpenD K-line: {code} {day}")
            rows[day] = row
        if next_key is None:
            break
        if next_key == key or next_key in seen_keys:
            raise ValueError("repeated OpenD history page key")
        seen_keys.add(next_key)
        key = next_key
    return rows


def _actions(client: object, code: str) -> dict[str, tuple[float, float]]:
    status, frame = client.get_rehab(code)
    if status != 0:
        raise RuntimeError(f"OpenD corporate action request failed for {code}: {str(frame)[:160]}")
    actions: dict[str, tuple[float, float]] = {}
    for row in _records(frame):
        day = str(row["ex_div_date"])[:10]
        date.fromisoformat(day)
        for unsupported in ("allotment_ratio", "spin_off_ratio", "stk_spo_ratio",
                            "per_share_div_ratio", "per_share_trans_ratio"):
            if nonnegative(row.get(unsupported)):
                raise ValueError(f"unsupported corporate action {unsupported} for {code} {day}")
        factor = nonnegative(row.get("split_ratio")) or 1.0
        dividend = nonnegative(row.get("per_cash_div")) + nonnegative(row.get("special_dividend"))
        old_factor, old_dividend = actions.get(day, (1.0, 0.0))
        actions[day] = (old_factor * factor, old_dividend + dividend)
    return actions


def export_rows(client: object, codes: list[str], start: str, end: str) -> list[dict]:
    """Fetch raw and forward-adjusted closes with explicit action checks."""
    result = []
    for code in codes:
        raw = _history(client, code, start, end, "None")
        adjusted = _history(client, code, start, end, "qfq")
        if not raw or raw.keys() != adjusted.keys():
            raise ValueError(f"raw/adjusted history mismatch or empty history for {code}")
        actions = _actions(client, code)
        for day, bar in sorted(raw.items()):
            split, dividend = actions.get(day, (1.0, 0.0))
            result.append({"date": day, "symbol": code.removeprefix("US."),
                           "open": positive(bar["open"], "open"),
                           "close": positive(bar["close"], "close"),
                           "adj_close": positive(adjusted[day]["close"], "adj_close"),
                           "split_factor": split, "dividend": dividend})
    return sorted(result, key=lambda row: (row["date"], row["symbol"]))


def save(rows: list[dict], out: Path, metadata: dict) -> None:
    if out.exists() or out.with_suffix(out.suffix + ".json").exists():
        raise FileExistsError("output exists; refusing to overwrite previous export")
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".moomoo-", suffix=".csv", dir=out.parent)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        with open(temporary, "rb") as handle:
            metadata["csvSha256"] = hashlib.file_digest(handle, "sha256").hexdigest()
        metadata["rows"] = len(rows)
        out.with_suffix(out.suffix + ".json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
        os.replace(temporary, out)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local, read-only OpenD US daily history export")
    parser.add_argument("--codes", required=True, help="Comma-separated US symbols, e.g. AAPL,MSFT,SPY")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if start > end:
        parser.error("start must not exceed end")
    codes = sorted({"US." + token.strip().upper().removeprefix("US.")
                    for token in args.codes.split(",") if token.strip()})
    if not codes or any(not code[3:].replace(".", "").isalnum() for code in codes):
        parser.error("provide valid US symbols")
    try:
        with socket.create_connection(("127.0.0.1", 11111), timeout=3):
            pass
    except OSError:
        parser.exit(2, "OpenD is not reachable at 127.0.0.1:11111 on this machine. Start and log in to OpenD locally.\n")
    from moomoo import OpenQuoteContext

    client = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        rows = export_rows(client, codes, args.start, args.end)
    finally:
        client.close()
    save(rows, args.out, {"source": "LOCAL_MOOMOO_OPEND_READ_ONLY", "codes": codes,
                          "start": args.start, "end": args.end,
                          "historicalUniverseIncluded": False,
                          "warning": "Current symbol list cannot establish historical Top-50 eligibility or executable fills."})
    print(f"Exported {len(rows)} rows to {args.out}; historical eligibility remains required.")


if __name__ == "__main__":
    main()
