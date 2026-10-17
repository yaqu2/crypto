"""Deterministic paper fills from quote snapshots, never broker orders."""
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
import json
import math
import os


def valid(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


class PaperEngine:
    def __init__(self, state_path: Path, demo=False):
        self.path = Path(state_path)
        self.demo = demo
        self.lock = RLock()
        self.account = {"cash": 100000.0, "positions": {}, "trades": [], "cooldowns": {},
                        "day": "", "dayStartEquity": 100000.0}
        if self.path.exists():
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if saved.get("demo") != demo:
                raise ValueError("Paper state belongs to a different feed mode")
            self.account = saved["account"]
            self.account.setdefault("cooldowns", {})
        self.running = False  # always require a fresh click after process start
        self.scan = None
        self.error = None
        self.opportunities = []
        self.direction = "UNKNOWN"
        self.gate = "WAITING_FOR_SCAN"
        self.updated = None

    def persist(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"demo": self.demo, "account": self.account},
                                  ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, self.path)

    def control(self, action):
        with self.lock:
            if action not in ("start", "pause"):
                raise ValueError("unknown action")
            self.running = action == "start"
            return self.state()

    def _equity(self, prices=None):
        prices = prices or {}
        return self.account["cash"] + sum(p["qty"] * (prices.get(code) or p["last"])
                                               for code, p in self.account["positions"].items())

    def _fill(self, code, qty, mid, side, reason, at):
        # 10 bps adverse fill + 2 bps explicit fee each side; no actual liquidity check.
        price = round(mid * (1.001 if side == "BUY" else 0.999), 4)
        gross = round(qty * price, 2)
        fee = round(gross * 0.0002, 2)
        if side == "BUY":
            if gross + fee > self.account["cash"]:
                return
            self.account["cash"] = round(self.account["cash"] - gross - fee, 2)
            self.account["positions"][code] = {"qty": qty, "entry": price,
                                                  "peak": mid, "last": mid, "opened": at}
        else:
            self.account["cash"] = round(self.account["cash"] + gross - fee, 2)
            del self.account["positions"][code]
            self.account["cooldowns"][code] = at
        self.account["trades"].append({"at": at, "code": code, "side": side,
                                       "qty": qty, "price": price, "fee": fee,
                                       "reason": reason, "simulated": True})
        self.account["trades"] = self.account["trades"][-300:]

    def apply(self, scan):
        with self.lock:
            self.scan = scan
            self.error = None
            self.updated = datetime.now(timezone.utc).isoformat()
            rows = scan.get("rows", [])
            quotes = {r.get("code"): r for r in rows if r.get("code")}
            usable = [r for r in rows if r.get("usable") and r.get("fresh")]
            breadth = sum((valid(r.get("changePct")) or 0) > 0 for r in usable) / len(usable) if usable else 0
            bench = scan.get("benchmarks", {})
            spy = bench.get("SPY", {})
            qqq = bench.get("QQQ", {})
            benchmarks_ok = all(x.get("usable") and x.get("fresh") for x in (spy, qqq))
            if scan.get("synthetic"):
                benchmarks_ok = all(valid(x.get("changePct")) is not None for x in (spy, qqq))
            up = benchmarks_ok and all((valid(x.get("changePct")) or 0) > 0.2 for x in (spy, qqq)) and breadth > 0.52
            down = benchmarks_ok and all((valid(x.get("changePct")) or 0) < -0.2 for x in (spy, qqq)) and breadth < 0.48
            self.direction = "BULL" if up else "BEAR" if down else "MIXED" if benchmarks_ok else "UNKNOWN"
            coverage = scan.get("usable", 0) / scan["universe"] if scan.get("universe") else 0
            if not scan.get("regular"):
                self.gate = "OUTSIDE_REGULAR_SESSION"
            elif not scan.get("complete") or coverage < 0.8 or scan.get("errors"):
                self.gate = "INCOMPLETE_OR_STALE_MARKET_SCAN"
            elif not benchmarks_ok:
                self.gate = "BENCHMARK_QUOTES_MISSING_OR_STALE"
            elif self.direction != "BULL":
                self.gate = "MARKET_DIRECTION_NOT_BULL"
            else:
                self.gate = "READY"
            candidates = []
            for r in usable:
                price, change, turnover, spread = (valid(r.get(k)) for k in
                                                    ("lastPrice", "changePct", "turnover", "spreadBps"))
                if (price is None or change is None or turnover is None or spread is None or
                        not 5 <= price <= 500 or not 1 <= change <= 8 or
                        turnover < 5_000_000 or spread > 30):
                    continue
                candidates.append({"code": r["code"], "name": str(r.get("name") or r["code"])[:80],
                                   "price": price, "changePct": change,
                                   "turnover": turnover, "spreadBps": spread,
                                   "score": round(change * 10 + min(turnover / 10_000_000, 20) - spread / 10, 2)})
            candidates.sort(key=lambda r: (-r["score"], r["code"]))
            self.opportunities = candidates[:12]
            for code, position in self.account["positions"].items():
                quote = quotes.get(code)
                price = valid(quote.get("lastPrice")) if quote and quote.get("usable") and quote.get("fresh") else None
                if price is not None and price > 0:
                    position["last"] = price
                    position["peak"] = max(position["peak"], price)
            # A quote cannot become a paper fill unless this scan passed every gate.
            if (not self.running or self.gate in ("OUTSIDE_REGULAR_SESSION",
                                                 "BENCHMARK_QUOTES_MISSING_OR_STALE") or
                    self.gate.startswith("INCOMPLETE")):
                self.persist()
                return
            now = datetime.fromisoformat(scan["at"])
            day = now.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York")).date().isoformat()
            if self.account["day"] != day:
                self.account["day"] = day
                self.account["dayStartEquity"] = self._equity()
                self.account["cooldowns"] = {}
            for code, position in list(self.account["positions"].items()):
                quote = quotes.get(code)
                if not quote or not quote.get("usable") or not quote.get("fresh"):
                    continue
                price = valid(quote.get("lastPrice"))
                if price is None or price <= 0:
                    continue
                position["last"] = price
                position["peak"] = max(position["peak"], price)
                opened = datetime.fromisoformat(position["opened"])
                age = (now - opened).total_seconds()
                reason = ("STOP_3_PERCENT" if price <= position["entry"] * 0.97 else
                          "TRAIL_2_PERCENT" if price <= position["peak"] * 0.98 else
                          "TAKE_5_PERCENT" if price >= position["entry"] * 1.05 else
                          "MAX_HOLD_2_DAYS" if age >= 172800 else
                          "MARKET_REGIME" if self.direction != "BULL" else None)
                if reason:
                    self._fill(code, position["qty"], price, "SELL", reason, scan["at"])
            equity = self._equity()
            daily_loss = equity < self.account["dayStartEquity"] * 0.98
            if daily_loss:
                self.gate = "DAILY_LOSS_LIMIT"
            if self.gate == "READY" and not daily_loss:
                for candidate in candidates:
                    if len(self.account["positions"]) >= 3:
                        break
                    code = candidate["code"]
                    if code in self.account["positions"] or code in self.account["cooldowns"]:
                        continue
                    budget = min(equity * 0.15, self.account["cash"] * 0.95)
                    qty = int(budget / (candidate["price"] * 1.0012))
                    if qty > 0:
                        self._fill(code, qty, candidate["price"], "BUY", "MOMENTUM_SCREEN", scan["at"])
            self.persist()

    def state(self):
        with self.lock:
            scan = self.scan or {}
            return {"mode": "SYNTHETIC_DEMO" if self.demo else "LOCAL_MOOMOO_OPEND",
                    "running": self.running, "updated": self.updated,
                    "direction": self.direction, "gate": self.gate,
                    "error": self.error, "coverage": {
                        "universe": scan.get("universe", 0), "received": scan.get("received", 0),
                        "usable": scan.get("usable", 0), "complete": scan.get("complete", False),
                        "regular": scan.get("regular", False), "errors": scan.get("errors", [])[:3]},
                    "benchmarks": scan.get("benchmarks", {}),
                    "opportunities": self.opportunities,
                    "account": {"cash": round(self.account["cash"], 2),
                                "equity": round(self._equity(), 2),
                                "dayStartEquity": self.account["dayStartEquity"],
                                "positions": self.account["positions"],
                                "trades": self.account["trades"][-20:][::-1]}}
