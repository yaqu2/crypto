"""Read-only quote feeds. The demo is synthetic and always identified as such."""
from datetime import datetime, timezone
import math
import random


class DemoFeed:
    label = "SYNTHETIC_DEMO"

    def __init__(self):
        self.tick = 0
        self.rng = random.Random(20261017)

    def scan(self):
        self.tick += 1
        rows = []
        for i in range(2400):
            code = f"US.DEMO{i:04d}"
            pre = 15 + i % 170
            change = (i % 31 - 12) / 200 + math.sin(self.tick / 6 + i) / 100
            price = round(pre * (1 + change), 2)
            rows.append({"code": code, "name": f"Synthetic {i:04d}", "lastPrice": price,
                         "previousClose": pre, "changePct": round(change * 100, 2),
                         "turnover": 2_000_000 + (i % 103) * 100_000,
                         "spreadBps": 8 + i % 14, "usable": True, "fresh": True})
        # Benchmarks are synthetic too; positive breadth permits testing the bot.
        return {"source": self.label, "at": datetime.now(timezone.utc).isoformat(),
                "universe": len(rows), "received": len(rows), "usable": len(rows),
                "complete": True, "regular": True, "rows": rows,
                "benchmarks": {"SPY": {"changePct": 0.7}, "QQQ": {"changePct": 1.0}},
                "errors": [], "synthetic": True}


class MoomooFeed:
    label = "LOCAL_MOOMOO_OPEND"

    def __init__(self):
        from moomoo import OpenQuoteContext, Market, SecurityType, RET_OK
        self.ok = RET_OK
        self.ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
        # The current directory changes over time; refresh it each scan.
        self.market, self.stock_type = Market.US, SecurityType.STOCK

    def close(self):
        self.ctx.close()

    def scan(self):
        import sys
        from pathlib import Path
        # Existing scanner is a script package with sibling imports.
        path = str(Path(__file__).resolve().parents[1] / "flowdesk")
        if path not in sys.path:
            sys.path.insert(0, path)
        from market_scan import (get_universe, exchange_scope, SnapshotPacer,
                                 fetch_batch, normalize_snapshot, summarize)
        from feed_guard import clock_state
        started = datetime.now(timezone.utc)
        universe = get_universe(self.ctx, self.market, self.stock_type, self.ok)
        listed = [item for item in universe if exchange_scope(item.get("exchange")) == "US_PRIMARY_LISTED"]
        errors, denials, quotes = [], [], []
        pacer = SnapshotPacer(self.ctx, 1.1, 100)
        for offset in range(0, len(listed), 400):
            quotes.extend(fetch_batch(pacer, [x["code"] for x in listed[offset:offset + 400]],
                                      self.ok, offset, errors, denials))
            if pacer.calls >= pacer.max_calls and offset + 400 < len(listed):
                errors.append({"reason": "SNAPSHOT_REQUEST_BUDGET_EXCEEDED"})
                break
        benchmarks = {}
        try:
            result, data = pacer.request(["US.SPY", "US.QQQ"])
            if result == self.ok:
                for raw in data.to_dict("records"):
                    quote = normalize_snapshot(raw, datetime.now(timezone.utc))
                    benchmarks[quote["code"].removeprefix("US.")] = quote
            else:
                errors.append({"reason": "BENCHMARK_REQUEST_FAILED: " + str(data)[:140]})
        except Exception as exc:
            errors.append({"reason": "BENCHMARK_REQUEST_FAILED: " + str(exc)[:140]})
        ended = datetime.now(timezone.utc)
        summary = summarize(universe, quotes, started, ended, errors, denials=denials,
                            requested_codes=[x["code"] for x in listed])
        names = {x["code"]: x["name"] for x in listed}
        for row in quotes:
            row["name"] = names.get(row["code"], row["code"])
        return {"source": self.label, "at": ended.isoformat(),
                "universe": len(listed), "received": summary["listedSnapshotCount"],
                "usable": summary["listedFreshUsableCount"],
                "complete": bool(summary["fullListedUniverse"] and len(listed) >= 2000),
                "regular": clock_state(ended)["regular"], "rows": quotes,
                "benchmarks": benchmarks, "errors": errors + denials, "synthetic": False}
