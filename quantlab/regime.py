"""Descriptive SPY/QQQ daily trend state known by the prior close."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backtest import Bar


def classify(days: list[date], data: dict[date, dict[str, Bar]], asof_index: int) -> str:
    """BULL/BEAR/MIXED/UNKNOWN from 20-session mean and 5-session return.

    `asof_index` is the latest completed session, never today's future close.
    """
    if asof_index < 20:
        return "UNKNOWN"
    votes = []
    for symbol in ("SPY", "QQQ"):
        bars = [data[day].get(symbol) for day in days[asof_index - 19:asof_index + 1]]
        if not all(bars):
            return "UNKNOWN"
        closes = [bar.adj_close for bar in bars]
        average = sum(closes) / 20
        if closes[-1] > average and closes[-1] > closes[-6]:
            votes.append("BULL")
        elif closes[-1] < average and closes[-1] < closes[-6]:
            votes.append("BEAR")
        else:
            votes.append("MIXED")
    return votes[0] if votes[0] == votes[1] and votes[0] != "MIXED" else "MIXED"
