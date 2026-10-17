"""Deterministic paper fills from quote snapshots, never broker orders."""
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from zoneinfo import ZoneInfo
import json
import math
import os


def valid(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


MARKET_TZ = ZoneInfo("America/New_York")


def aware_time(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo and parsed.utcoffset() is not None else None
    except (TypeError, ValueError):
        return None


class PaperEngine:
    def __init__(self, state_path: Path, demo=False):
        self.path = Path(state_path)
        self.demo = demo
        self.lock = RLock()
        self.account = {"cash": 100000.0, "positions": {}, "trades": [], "cooldowns": {},
                        "day": "", "dayStartEquity": 100000.0,
                        "haltDay": "", "lastAppliedAt": "", "pending": []}
        if self.path.exists():
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if saved.get("demo") != demo:
                raise ValueError("Paper state belongs to a different feed mode")
            self.account = saved["account"]
            self.account.setdefault("cooldowns", {})
            self.account.setdefault("haltDay", "")
            self.account.setdefault("pending", [])
            if "lastAppliedAt" not in self.account:
                seen = [aware_time(t.get("at")) for t in (self.account.get("trades")
                        if isinstance(self.account.get("trades"), list) else [])
                        if isinstance(t, dict)]
                self.account["lastAppliedAt"] = max((t for t in seen if t), default=None)
                self.account["lastAppliedAt"] = (self.account["lastAppliedAt"].isoformat()
                                                 if self.account["lastAppliedAt"] else "")
            self._validate_account()
        self.running = False  # always require a fresh click after process start
        self.scan = None
        self.error = None
        self.opportunities = []
        self.direction = "UNKNOWN"
        self.gate = "WAITING_FOR_SCAN"
        self.updated = None

    def _validate_account(self):
        a = self.account
        if (not isinstance(a, dict) or type(a.get("cash")) not in (int, float) or
                valid(a.get("cash")) is None or a["cash"] < 0 or
                type(a.get("dayStartEquity")) not in (int, float) or
                valid(a.get("dayStartEquity")) is None or a["dayStartEquity"] <= 0 or
                not isinstance(a.get("positions"), dict) or not isinstance(a.get("trades"), list) or
                not isinstance(a.get("pending"), list) or
                not isinstance(a.get("cooldowns"), dict) or
                not isinstance(a.get("day"), str) or not isinstance(a.get("haltDay"), str) or
                not isinstance(a.get("lastAppliedAt"), str) or
                (a["lastAppliedAt"] and not aware_time(a["lastAppliedAt"]))):
            raise ValueError("Invalid paper account state")
        for code, p in a["positions"].items():
            if (not isinstance(code, str) or not isinstance(p, dict) or
                    type(p.get("qty")) is not int or p["qty"] <= 0 or
                    any(type(p.get(key)) not in (int, float) or valid(p.get(key)) is None or p[key] <= 0
                        for key in ("entry", "peak", "last")) or not aware_time(p.get("opened"))):
                raise ValueError("Invalid paper position")
        for signal in a["pending"]:
            if (not isinstance(signal, dict) or not isinstance(signal.get("code"), str) or
                    signal.get("side") not in ("BUY", "SELL") or
                    not aware_time(signal.get("signalAt")) or
                    not isinstance(signal.get("reason"), str)):
                raise ValueError("Invalid pending paper signal")

    def persist(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as output:
            json.dump({"demo": self.demo, "account": self.account}, output,
                      ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, self.path)

    def control(self, action):
        with self.lock:
            if action not in ("start", "pause"):
                raise ValueError("unknown action")
            if action == "pause":
                # Stop in memory first: a full disk must not leave the bot live.
                self.running = False
            if (action == "pause" or not self.running) and self.account["pending"]:
                # A restart must never execute a signal from the previous run.
                self.account["pending"] = []
                self.persist()
            if action == "start":
                self.running = True
            return self.state()

    def _equity(self):
        return self.account["cash"] + sum(p["qty"] * p["last"]
                                               for p in self.account["positions"].values())

    def _execution_price(self, quote, side):
        if not isinstance(quote, dict) or not quote.get("usable") or not quote.get("fresh"):
            return None
        mid = valid(quote.get("lastPrice"))
        if mid is None or mid <= 0:
            return None
        if self.demo:
            spread = valid(quote.get("spreadBps"))
            if spread is None or spread < 0:
                return None
            reference = mid * (1 + spread / 20000) if side == "BUY" else mid * (1 - spread / 20000)
            model = "SYNTHETIC_SPREAD_PLUS_SLIPPAGE"
        else:
            bid, ask = valid(quote.get("bidPrice")), valid(quote.get("askPrice"))
            if (not quote.get("bboValid") or bid is None or ask is None or
                    bid <= 0 or ask <= bid):
                return None
            reference = ask if side == "BUY" else bid
            model = "DISPLAYED_BBO_PLUS_SLIPPAGE"
        fill = round(reference * (1.001 if side == "BUY" else 0.999), 4)
        return (fill, reference, model) if fill > 0 else None

    def _spread_bps(self, quote):
        if self.demo:
            return valid(quote.get("spreadBps"))
        bid, ask = valid(quote.get("bidPrice")), valid(quote.get("askPrice"))
        if not quote.get("bboValid") or bid is None or ask is None or bid <= 0 or ask <= bid:
            return None
        return (ask - bid) / ((ask + bid) / 2) * 10000

    def _fill(self, code, qty, quote, side, reason, at, signal_at=None):
        # Displayed BBO is not a guaranteed broker fill or consolidated NBBO.
        execution = self._execution_price(quote, side)
        if execution is None:
            return False
        price, reference, model = execution
        gross = round(qty * price, 2)
        fee = round(gross * 0.0002, 2)
        if side == "BUY":
            if gross + fee > self.account["cash"] or code in self.account["positions"]:
                return False
            self.account["cash"] = round(self.account["cash"] - gross - fee, 2)
            liquidation = self._execution_price(quote, "SELL")
            self.account["positions"][code] = {"qty": qty, "entry": price,
                                                  "peak": quote["lastPrice"],
                                                  "last": liquidation[0] * 0.9998,
                                                  "opened": at}
        else:
            if code not in self.account["positions"] or qty != self.account["positions"][code]["qty"]:
                return False
            self.account["cash"] = round(self.account["cash"] + gross - fee, 2)
            del self.account["positions"][code]
            self.account["cooldowns"][code] = at
        trade = {"at": at, "code": code, "side": side,
                                       "qty": qty, "price": price, "fee": fee,
                                       "referencePrice": round(reference, 4),
                                       "slippageBps": 10, "fillModel": model,
                                       "reason": reason, "simulated": True}
        if signal_at is not None:
            trade["signalAt"] = signal_at
            trade["quoteAsOf"] = quote.get("sourceAsOf")
        self.account["trades"].append(trade)
        return True

    def apply(self, scan):
        with self.lock:
            self.scan = scan
            self.error = None
            self.updated = datetime.now(timezone.utc).isoformat()
            at = aware_time(scan.get("at"))
            last_at = aware_time(self.account["lastAppliedAt"])
            age = (datetime.now(timezone.utc) - at).total_seconds() if at else None
            if at is None or age < -5 or (not self.demo and age > 180):
                self.gate = "INVALID_OR_STALE_SCAN_TIME"
                self.account["pending"] = []
                self.persist()
                return
            if last_at and at <= last_at:
                self.gate = "DUPLICATE_OR_OUT_OF_ORDER_SCAN"
                self.account["pending"] = []
                self.persist()
                return
            self.account["lastAppliedAt"] = at.isoformat()
            rows = scan.get("rows", [])
            if not isinstance(rows, list):
                self.gate = "INVALID_MARKET_SCAN"
                self.account["pending"] = []
                self.persist()
                return
            quotes = {r.get("code"): r for r in rows if isinstance(r, dict) and r.get("code")}
            usable = [r for r in quotes.values() if r.get("usable") and r.get("fresh")]
            breadth = sum((valid(r.get("changePct")) or 0) > 0 for r in usable) / len(usable) if usable else 0
            bench = scan.get("benchmarks", {})
            spy = bench.get("SPY", {})
            qqq = bench.get("QQQ", {})
            benchmarks_ok = all(isinstance(x, dict) and x.get("usable") and x.get("fresh") and
                                valid(x.get("lastPrice")) is not None and valid(x.get("lastPrice")) > 0
                                for x in (spy, qqq))
            if self.demo:
                benchmarks_ok = all(valid(x.get("changePct")) is not None for x in (spy, qqq))
            elif benchmarks_ok:
                benchmarks_ok = all(self._quote_current(x, at) for x in (spy, qqq))
            up = benchmarks_ok and all((valid(x.get("changePct")) or 0) > 0.2 for x in (spy, qqq)) and breadth > 0.52
            down = benchmarks_ok and all((valid(x.get("changePct")) or 0) < -0.2 for x in (spy, qqq)) and breadth < 0.48
            self.direction = "BULL" if up else "BEAR" if down else "MIXED" if benchmarks_ok else "UNKNOWN"
            universe = scan.get("universe", 0)
            coverage = len(usable) / universe if type(universe) is int and universe > 0 else 0
            counts_ok = (type(universe) is int and universe > 0 and
                         scan.get("received") == len(quotes) and scan.get("usable") == len(usable) and
                         len(quotes) <= universe and bool(scan.get("synthetic")) == self.demo and
                         (self.demo or universe >= 2000))
            if not scan.get("regular"):
                self.gate = "OUTSIDE_REGULAR_SESSION"
            elif not counts_ok or not scan.get("complete") or coverage < 0.8 or scan.get("errors"):
                self.gate = "INCOMPLETE_OR_STALE_MARKET_SCAN"
            elif not benchmarks_ok:
                self.gate = "BENCHMARK_QUOTES_MISSING_OR_STALE"
            elif self.direction != "BULL":
                self.gate = "MARKET_DIRECTION_NOT_BULL"
            else:
                self.gate = "READY"
            candidates = []
            for r in usable:
                price, change, turnover = (valid(r.get(k)) for k in
                                           ("lastPrice", "changePct", "turnover"))
                spread = self._spread_bps(r)
                if (price is None or change is None or turnover is None or spread is None or
                        not 5 <= price <= 500 or not 1 <= change <= 8 or
                        turnover < 5_000_000 or spread < 0 or spread > 30 or
                        (not self.demo and not self._quote_current(r, at)) or
                        (not self.demo and not (r["bidPrice"] * 0.98 <= price <=
                                               r["askPrice"] * 1.02)) or
                        self._execution_price(r, "BUY") is None):
                    continue
                candidates.append({"code": r["code"], "name": str(r.get("name") or r["code"])[:80],
                                   "price": price, "changePct": change,
                                   "turnover": turnover, "spreadBps": spread,
                                   "score": round(change * 10 + min(turnover / 10_000_000, 20) - spread / 10, 2)})
            candidates.sort(key=lambda r: (-r["score"], r["code"]))
            self.opportunities = candidates[:12]
            previous_equity = self._equity()
            for code, position in self.account["positions"].items():
                quote = quotes.get(code)
                price = valid(quote.get("lastPrice")) if quote and quote.get("usable") and quote.get("fresh") and (self.demo or self._quote_current(quote, at)) else None
                if price is not None and price > 0:
                    position["peak"] = max(position["peak"], price)
                    liquidation = self._execution_price(quote, "SELL")
                    if liquidation:
                        position["last"] = liquidation[0] * 0.9998
            day = at.astimezone(MARKET_TZ).date().isoformat()
            if self.account["day"] != day:
                self.account["day"] = day
                # Carry the last known prior-day valuation across the session
                # boundary, so an overnight gap counts toward today's limit.
                self.account["dayStartEquity"] = previous_equity
                self.account["cooldowns"] = {}
                self.account["haltDay"] = ""
            if self._equity() < self.account["dayStartEquity"] * 0.98:
                self.account["haltDay"] = day
            if self.account["haltDay"] == day and self.gate == "READY":
                self.gate = "DAILY_LOSS_LIMIT"
            # A quote cannot become a paper fill unless this scan passed every gate.
            if (not self.running or self.gate in ("OUTSIDE_REGULAR_SESSION",
                                                 "BENCHMARK_QUOTES_MISSING_OR_STALE") or
                    self.gate.startswith("INCOMPLETE")):
                self.account["pending"] = []
                self.persist()
                return
            now = at
            if not self.demo:
                self._process_pending(quotes, candidates, at, day)
                if self._equity() < self.account["dayStartEquity"] * 0.98:
                    self.account["haltDay"] = day
                    self.gate = "DAILY_LOSS_LIMIT"
                for code, position in self.account["positions"].items():
                    quote = quotes.get(code)
                    if quote and self._quote_current(quote, at):
                        reason = self._exit_reason(position, quote, at)
                        if reason:
                            self.account["pending"].append({"code": code, "side": "SELL",
                                                            "reason": reason,
                                                            "signalAt": at.isoformat()})
                if self.gate == "READY" and self.account["haltDay"] != day:
                    free_slots = 3 - len(self.account["positions"])
                    for candidate in candidates:
                        if free_slots <= 0:
                            break
                        code = candidate["code"]
                        if code in self.account["positions"] or code in self.account["cooldowns"]:
                            continue
                        self.account["pending"].append({"code": code, "side": "BUY",
                                                        "reason": "MOMENTUM_SCREEN",
                                                        "signalAt": at.isoformat()})
                        free_slots -= 1
                self.persist()
                return
            for code, position in list(self.account["positions"].items()):
                quote = quotes.get(code)
                if not quote or not quote.get("usable") or not quote.get("fresh") or (not self.demo and not self._quote_current(quote, at)):
                    continue
                price = valid(quote.get("lastPrice"))
                if price is None or price <= 0:
                    continue
                liquidation = self._execution_price(quote, "SELL")
                if liquidation is None:
                    continue
                position["last"] = liquidation[0] * 0.9998
                position["peak"] = max(position["peak"], price)
                opened = datetime.fromisoformat(position["opened"])
                age = (now - opened).total_seconds()
                exit_signal_price = price if self.demo else min(price, quote["bidPrice"])
                reason = ("STOP_3_PERCENT" if exit_signal_price <= position["entry"] * 0.97 else
                          "TRAIL_2_PERCENT" if exit_signal_price <= position["peak"] * 0.98 else
                          "TAKE_5_PERCENT" if exit_signal_price >= position["entry"] * 1.05 else
                          "MAX_HOLD_2_DAYS" if age >= 172800 else
                          "MARKET_REGIME" if self.direction != "BULL" else None)
                if reason:
                    self._fill(code, position["qty"], quote, "SELL", reason, at.isoformat())
            equity = self._equity()
            if equity < self.account["dayStartEquity"] * 0.98:
                self.account["haltDay"] = day
            daily_loss = self.account["haltDay"] == day
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
                    quote = quotes[code]
                    execution = self._execution_price(quote, "BUY")
                    if execution is None:
                        continue
                    qty = int(budget / (execution[0] * 1.0002))
                    if qty > 0:
                        self._fill(code, qty, quote, "BUY", "MOMENTUM_SCREEN", at.isoformat())
            self.persist()

    def _quote_current(self, quote, at):
        source = aware_time(quote.get("sourceAsOf"))
        return bool(source and -5 <= (at - source).total_seconds() <= 180 and
                    source.astimezone(MARKET_TZ).date() == at.astimezone(MARKET_TZ).date())

    def _exit_reason(self, position, quote, at):
        if self._execution_price(quote, "SELL") is None:
            return None
        price, bid = valid(quote.get("lastPrice")), valid(quote.get("bidPrice"))
        if price is None or bid is None:
            return None
        exit_price = min(price, bid)
        age = (at - datetime.fromisoformat(position["opened"])).total_seconds()
        return ("STOP_3_PERCENT" if exit_price <= position["entry"] * 0.97 else
                "TRAIL_2_PERCENT" if exit_price <= position["peak"] * 0.98 else
                "TAKE_5_PERCENT" if exit_price >= position["entry"] * 1.05 else
                "MAX_HOLD_2_DAYS" if age >= 172800 else
                "MARKET_REGIME" if self.direction != "BULL" else None)

    def _process_pending(self, quotes, candidates, at, day):
        """Consume one prior full scan's signals, never a current-scan signal."""
        pending = self.account["pending"]
        self.account["pending"] = []
        candidate_codes = {c["code"] for c in candidates}
        for signal in sorted(pending, key=lambda s: s["side"] != "SELL"):
            signal_at = aware_time(signal["signalAt"])
            quote = quotes.get(signal["code"])
            source = aware_time(quote.get("sourceAsOf")) if quote else None
            if (signal_at is None or at <= signal_at or
                    (at - signal_at).total_seconds() > 300 or
                    source is None or source <= signal_at or
                    (source - signal_at).total_seconds() > 300 or
                    not self._quote_current(quote, at) or
                    self._execution_price(quote, signal["side"]) is None):
                continue
            code = signal["code"]
            if signal["side"] == "SELL":
                position = self.account["positions"].get(code)
                if position:
                    self._fill(code, position["qty"], quote, "SELL", signal["reason"],
                               at.isoformat(), signal["signalAt"])
                    if self._equity() < self.account["dayStartEquity"] * 0.98:
                        self.account["haltDay"] = day
            elif (self.gate == "READY" and self.account["haltDay"] != day and
                  code in candidate_codes and code not in self.account["positions"] and
                  code not in self.account["cooldowns"] and len(self.account["positions"]) < 3):
                execution = self._execution_price(quote, "BUY")
                equity = self._equity()
                budget = min(equity * 0.15, self.account["cash"] * 0.95)
                qty = int(budget / (execution[0] * 1.0002))
                if qty > 0:
                    self._fill(code, qty, quote, "BUY", signal["reason"],
                               at.isoformat(), signal["signalAt"])

    def state(self):
        with self.lock:
            scan = self.scan or {}
            return {"mode": "SYNTHETIC_DEMO" if self.demo else "LOCAL_MOOMOO_OPEND",
                    "fillTiming": "SAME_SYNTHETIC_SCAN" if self.demo else "NEXT_QUALIFIED_SCAN",
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
                                "pending": self.account["pending"],
                                "trades": self.account["trades"][-20:][::-1]}}
