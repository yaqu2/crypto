"""Point-in-time US equity momentum portfolio simulator; no live orders."""

from __future__ import annotations

import csv
import hashlib
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class Bar:
    day: date
    symbol: str
    open: float
    close: float
    adj_close: float
    split_factor: float
    dividend: float
    eligible: bool


def load_bars(path: Path) -> tuple[list[date], dict[date, dict[str, Bar]]]:
    """Require explicit historical eligibility and raw/adjusted prices."""
    columns = {"date", "symbol", "open", "close", "adj_close", "split_factor", "dividend", "eligible"}
    data: dict[date, dict[str, Bar]] = {}
    with path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames or not columns.issubset(reader.fieldnames):
            raise ValueError("CSV requires date,symbol,open,close,adj_close,split_factor,dividend,eligible")
        for row in reader:
            try:
                day = date.fromisoformat(row["date"])
                symbol = row["symbol"].strip().upper()
                prices = [float(row[name]) for name in ("open", "close", "adj_close", "split_factor", "dividend")]
            except (TypeError, ValueError, AttributeError) as exc:
                raise ValueError("invalid date, symbol or price") from exc
            if not symbol or any(not math.isfinite(x) for x in prices) or any(x <= 0 for x in prices[:4]) or prices[4] < 0:
                raise ValueError("invalid symbol, price, split factor or dividend")
            if row["eligible"] not in ("0", "1"):
                raise ValueError("eligible must be 0 or 1 as of that session")
            daily = data.setdefault(day, {})
            if symbol in daily:
                raise ValueError(f"duplicate symbol/session: {symbol} {day}")
            daily[symbol] = Bar(day, symbol, *prices, row["eligible"] == "1")
    if not data:
        raise ValueError("CSV has no bars")
    return sorted(data), data


def backtest(days: list[date], data: dict[date, dict[str, Bar]], *, lookback: int = 60,
             top_n: int = 50, fee_bps: float = 10, slippage_bps: float = 5,
             initial_cash: float = 5000) -> dict:
    if lookback < 1 or top_n < 1 or len(days) < lookback + 2:
        raise ValueError("need positive lookback/top_n and at least lookback + 2 sessions")
    if not math.isfinite(initial_cash) or initial_cash <= 0 or any(
        not math.isfinite(x) or x < 0 or x >= 10000 for x in (fee_bps, slippage_bps)
    ):
        raise ValueError("invalid cash or costs")
    fee, slip = fee_bps / 10000, slippage_bps / 10000
    cash = initial_cash
    shares: dict[str, float] = {}
    trades, daily = [], []
    for i, day in enumerate(days):
        bars = data[day]
        selected: list[str] = []
        if i >= lookback + 1:
            previous, anchor = data[days[i - 1]], data[days[i - 1 - lookback]]
            ranks = [(bar.adj_close / anchor[symbol].adj_close - 1, symbol)
                     for symbol, bar in previous.items() if bar.eligible and symbol in anchor]
            if len(ranks) < top_n:
                raise ValueError(f"eligible historical universe has {len(ranks)} ranked symbols, below top_n={top_n}")
            selected = [s for _, s in sorted(ranks, key=lambda pair: (-pair[0], pair[1]))[:top_n]]
        missing = (set(shares) | set(selected)) - bars.keys()
        if missing:
            raise ValueError(f"missing current raw bar on {day}: {','.join(sorted(missing))}")
        for symbol in shares:
            shares[symbol] *= bars[symbol].split_factor
        dividend_due = sum(qty * bars[s].dividend for s, qty in shares.items())
        open_equity = cash + sum(qty * bars[s].open for s, qty in shares.items())
        target = open_equity / len(selected) if selected else 0.0

        def record(symbol: str, side: str, qty: float, price: float) -> None:
            trades.append({"date": day.isoformat(), "symbol": symbol, "side": side,
                           "shares": round(qty, 8), "fillPrice": round(price, 8),
                           "fee": round(qty * price * fee, 8)})

        for symbol in sorted(shares):
            desired = target / bars[symbol].open if symbol in selected else 0.0
            qty = max(0.0, shares[symbol] - desired)
            if qty > 1e-12:
                price = bars[symbol].open * (1 - slip)
                cash += qty * price * (1 - fee)
                shares[symbol] -= qty
                record(symbol, "SELL", qty, price)
        for symbol in selected:
            price = bars[symbol].open * (1 + slip)
            desired = target / bars[symbol].open
            qty = min(max(0.0, desired - shares.get(symbol, 0.0)), cash / (price * (1 + fee)))
            if qty > 1e-12:
                cash -= qty * price * (1 + fee)
                shares[symbol] = shares.get(symbol, 0.0) + qty
                record(symbol, "BUY", qty, price)
        shares = {s: qty for s, qty in shares.items() if qty > 1e-12}
        cash += dividend_due  # ex-date cash is unavailable for opening orders
        equity = cash + sum(qty * bars[s].close for s, qty in shares.items())
        if i == len(days) - 1:
            for symbol, qty in sorted(shares.items()):
                price = bars[symbol].close * (1 - slip)
                cash += qty * price * (1 - fee)
                record(symbol, "SELL_FINAL_CLOSE", qty, price)
            equity = cash
        daily.append({"date": day.isoformat(), "selected": selected, "equity": round(equity, 6)})
    curve = [row["equity"] for row in daily]
    peak, drawdown = curve[0], 0.0
    for value in curve:
        peak = max(peak, value)
        drawdown = max(drawdown, 1 - value / peak)
    total = curve[-1] / initial_cash - 1
    elapsed = (days[-1] - days[0]).days
    spy = [data[day].get("SPY") for day in days]
    benchmark = round(spy[-1].adj_close / spy[0].adj_close - 1, 8) if all(spy) else None
    return {
        "period": {"start": days[0].isoformat(), "end": days[-1].isoformat(), "sessions": len(days)},
        "parameters": {"lookbackSessions": lookback, "topN": top_n, "feeBpsPerSide": fee_bps,
                       "slippageBpsPerSide": slippage_bps, "initialCash": initial_cash},
        "execution": "prior close ranking; next raw open rebalance; final raw close liquidation",
        "result": {"endingEquity": round(curve[-1], 6), "totalReturn": round(total, 8),
                   "annualizedReturn": round((1 + total) ** (365 / elapsed) - 1, 8) if elapsed else None,
                   "maxDrawdown": round(drawdown, 8), "orderCount": len(trades)},
        "spyAdjustedCloseReturnBeforeCosts": benchmark,
        "trades": trades, "daily": daily,
        "evidence": "SIMULATED_QUOTES_NO_BROKER_FILLS",
        "limitations": ["point-in-time eligibility and adjusted closes depend on supplied data",
                        "no partial fills, market impact, queue or tax model"],
    }


def run(path: Path, **kwargs) -> dict:
    days, data = load_bars(path)
    study = backtest(days, data, **kwargs)
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    return {"source": str(path), "sourceSha256": digest, "study": study}
