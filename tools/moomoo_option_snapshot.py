"""Read-only current option-chain snapshot from the same Mac as OpenD."""

import argparse
import json
import os
import socket
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from quantlab.option_walls import summarize


def collect(client: object, underlying: str, start: str, end: str) -> dict:
    ret, frame = client.get_option_chain(underlying, start=start, end=end)
    if ret != 0:
        raise RuntimeError(f"OpenD option-chain request failed: {str(frame)[:160]}")
    chain = frame.to_dict("records")
    if not chain or len(chain) > 2000:
        raise ValueError("option chain empty or above 2000 contracts; narrow the expiry range")
    ret, frame = client.get_market_snapshot([underlying])
    if ret != 0 or len(frame) != 1:
        raise RuntimeError("OpenD underlying snapshot unavailable")
    stock = frame.to_dict("records")[0]
    spot = float(stock["last_price"])
    lookup = {}
    codes = [row["code"] for row in chain]
    for offset in range(0, len(codes), 200):
        ret, frame = client.get_market_snapshot(codes[offset:offset + 200])
        if ret != 0:
            raise RuntimeError(f"OpenD option snapshot batch failed at index {offset}: {str(frame)[:160]}")
        for row in frame.to_dict("records"):
            lookup[row["code"]] = row
        if offset + 200 < len(codes):
            time.sleep(0.6)
    rows = []
    for item in chain:
        quote = lookup.get(item["code"], {})
        rows.append({"code": item["code"], "optionType": item["option_type"],
                     "expiry": str(item["strike_time"])[:10], "strike": item["strike_price"],
                     "openInterest": quote.get("open_interest"),
                     "contractSize": quote.get("contract_size"),
                     "sourceQuoteDate": quote.get("data_date"),
                     "sourceQuoteTime": quote.get("data_time")})
    captured = datetime.now(timezone.utc).isoformat()
    return {"source": "LOCAL_MOOMOO_OPEND_CURRENT_SNAPSHOT", "capturedAtUTC": captured,
            "underlying": underlying, "spot": spot,
            "spotSourceQuoteDate": stock.get("data_date"),
            "spotSourceQuoteTime": stock.get("data_time"),
            "chainCount": len(chain), "optionSnapshotCount": len(lookup),
            "rows": rows, "summary": summarize(rows, spot),
            "historicalPointInTimeSeries": False, "tradeEnabled": False}


def save_json(report: dict, out: Path) -> None:
    if out.exists():
        raise FileExistsError("output already exists; refusing to overwrite")
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".option-snapshot-", suffix=".json", dir=out.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, out)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> None:
    parser = argparse.ArgumentParser(description="Current OpenD option OI snapshot; no trade API")
    parser.add_argument("--underlying", required=True, help="US symbol, e.g. SPY")
    parser.add_argument("--days", type=int, default=14, help="up to 29 days of expiry dates")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not 0 <= args.days <= 29:
        parser.error("days must be between 0 and 29")
    symbol = args.underlying.strip().upper().removeprefix("US.")
    if not symbol or not symbol.replace(".", "").isalnum():
        parser.error("invalid US underlying")
    today = datetime.now(ZoneInfo("America/New_York")).date()
    try:
        with socket.create_connection(("127.0.0.1", 11111), timeout=3):
            pass
    except OSError:
        parser.exit(2, "OpenD is not reachable at 127.0.0.1:11111 on this machine.\n")
    from moomoo import OpenQuoteContext

    client = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        report = collect(client, "US." + symbol, today.isoformat(),
                         (today + timedelta(days=args.days)).isoformat())
    finally:
        client.close()
    save_json(report, args.out)
    print(f"Saved {report['chainCount']} option contracts; status={report['summary']['status']}")


if __name__ == "__main__":
    main()
