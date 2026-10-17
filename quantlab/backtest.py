"""Single-asset daily moving-average study with next-open execution.

This is a research simulator, not an exchange or broker execution model.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path


@dataclass(frozen=True)
class Bar:
    day: date
    open: float
    close: float


def load_bars(path: Path) -> list[Bar]:
    """Require a continuous UTC daily series; reject silent holes and bad prices."""
    with path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames or not {"date", "open", "close"}.issubset(reader.fieldnames):
            raise ValueError("CSV must contain date,open,close columns")
        bars = []
        for row in reader:
            try:
                bar = Bar(date.fromisoformat(row["date"]), float(row["open"]), float(row["close"]))
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid date or price in CSV") from exc
            if not all(math.isfinite(price) and price > 0 for price in (bar.open, bar.close)):
                raise ValueError("prices must be finite and positive")
            if bars and bar.day != bars[-1].day + timedelta(days=1):
                raise ValueError("dates must be strictly increasing with no missing UTC days")
            bars.append(bar)
    if not bars:
        raise ValueError("CSV has no bars")
    return bars


def _stats(equity: list[float], trades: int, initial_cash: float) -> dict:
    peak = equity[0]
    drawdown = 0.0
    for value in equity:
        peak = max(peak, value)
        drawdown = max(drawdown, 1 - value / peak)
    n = len(equity) - 1
    total = equity[-1] / initial_cash - 1
    return {
        "endingEquity": round(equity[-1], 6),
        "totalReturn": round(total, 8),
        "annualizedReturn": round((1 + total) ** (365 / n) - 1, 8) if n else None,
        "maxDrawdown": round(drawdown, 8),
        "orderCount": trades,
    }


def backtest(
    bars: list[Bar], *, short: int = 10, long: int = 30,
    fee_bps: float = 10, slippage_bps: float = 5,
    initial_cash: float = 1000, baseline: bool = False,
) -> dict:
    if not (1 <= short < long and len(bars) >= long + 2):
        raise ValueError("need 1 <= short < long and at least long + 2 daily bars")
    if initial_cash <= 0 or not all(math.isfinite(x) and 0 <= x < 10000 for x in (fee_bps, slippage_bps)):
        raise ValueError("cash and cost assumptions are invalid")
    fee, slip = fee_bps / 10000, slippage_bps / 10000
    cash, units, orders = initial_cash, 0.0, 0
    curve = []
    trades = []
    for i, bar in enumerate(bars):
        if baseline:
            want_asset = i > 0
        elif i >= long:
            known = bars[:i]  # yesterday's close is the newest available signal
            fast = sum(b.close for b in known[-short:]) / short
            slow = sum(b.close for b in known[-long:]) / long
            want_asset = fast > slow
        else:
            want_asset = False
        if want_asset and cash > 0:
            units = cash / (bar.open * (1 + slip) * (1 + fee))
            fill = bar.open * (1 + slip)
            trades.append({"date": bar.day.isoformat(), "side": "BUY", "fillPrice": round(fill, 8),
                           "units": round(units, 8), "fee": round(units * fill * fee, 8)})
            cash = 0.0
            orders += 1
        elif not want_asset and units > 0:
            fill = bar.open * (1 - slip)
            trades.append({"date": bar.day.isoformat(), "side": "SELL", "fillPrice": round(fill, 8),
                           "units": round(units, 8), "fee": round(units * fill * fee, 8)})
            cash += units * bar.open * (1 - slip) * (1 - fee)
            units = 0.0
            orders += 1
        curve.append(cash + units * bar.close)
    if units:
        fill = bars[-1].close * (1 - slip)
        trades.append({"date": bars[-1].day.isoformat(), "side": "SELL_FINAL_CLOSE",
                       "fillPrice": round(fill, 8), "units": round(units, 8),
                       "fee": round(units * fill * fee, 8)})
        cash += units * bars[-1].close * (1 - slip) * (1 - fee)
        orders += 1
        curve[-1] = cash
    return {
        "period": {"start": bars[0].day.isoformat(), "end": bars[-1].day.isoformat(), "bars": len(bars)},
        "parameters": {"short": short, "long": long, "feeBpsPerSide": fee_bps,
                       "slippageBpsPerSide": slippage_bps, "initialCash": initial_cash},
        "execution": "previous UTC close signal; next UTC open order; final close liquidation",
        "result": _stats(curve, orders, initial_cash),
        "trades": trades,
        "equityCurve": [{"date": b.day.isoformat(), "equity": round(e, 6)} for b, e in zip(bars, curve)],
        "evidence": "SIMULATED_QUOTES_NO_BROKER_FILLS",
    }


def run(path: Path, **kwargs) -> dict:
    bars = load_bars(path)
    strategy = backtest(bars, **kwargs)
    comparison = backtest(bars, **kwargs, baseline=True)
    with path.open("rb") as source:
        source_hash = hashlib.file_digest(source, "sha256").hexdigest()
    return {"source": str(path), "sourceSha256": source_hash,
            "strategy": strategy, "buyAndHold": comparison["result"],
            "warning": "Results depend on supplied prices and assumed costs; no live-trading authorization."}
