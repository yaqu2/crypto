"""Predeclared short-horizon US equity candidates and chronological holdout.

All candidates are reported. This module never promotes a winner or places an order.
"""

import argparse
import hashlib
import json
from datetime import date
from pathlib import Path

from .backtest import backtest, load_bars


PRESETS = (
    ("reversal_1d_hold1", 1, 1, "reversal", False, None),
    ("reversal_2d_hold2", 2, 2, "reversal", False, None),
    ("momentum_5d_hold2", 5, 2, "momentum", False, None),
    ("momentum_5d_bull_gate", 5, 2, "momentum", True, None),
    ("momentum_5d_trailing10", 5, 2, "momentum", False, 0.10),
    ("momentum_10d_hold5", 10, 5, "momentum", False, None),
    ("momentum_20d_hold10", 20, 10, "momentum", False, None),
    ("momentum_60d_daily_baseline", 60, 1, "momentum", False, None),
    ("momentum_60d_bull_gate", 60, 1, "momentum", True, None),
)


def _holdout_metrics(curve: list[float], start_index: int, initial_cash: float) -> dict:
    previous = curve[start_index - 1] if start_index else initial_cash
    values = [previous] + curve[start_index:]
    peak = values[0]
    drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        drawdown = max(drawdown, 1 - value / peak)
    return {"startingEquity": round(previous, 6), "endingEquity": round(values[-1], 6),
            "return": round(values[-1] / previous - 1, 8), "maxDrawdown": round(drawdown, 8)}


def compare(path: Path, *, test_start: date, top_n: int = 50,
            fee_bps: float = 10, slippage_bps: float = 5,
            initial_cash: float = 5000, min_test_sessions: int = 60) -> dict:
    days, data = load_bars(path)
    if test_start > days[-1]:
        raise ValueError("test_start is after the supplied data")
    start_index = next(i for i, day in enumerate(days) if day >= test_start)
    if start_index < 61:
        raise ValueError("test_start must follow at least 61 warmup sessions for the 60-day baseline")
    results = []
    for name, lookback, rebalance, mode, gate, stop in PRESETS:
        study = backtest(days, data, lookback=lookback, top_n=top_n,
                         rebalance_every=rebalance, rank_mode=mode,
                         regime_gate=gate, trailing_stop_pct=stop,
                         fee_bps=fee_bps, slippage_bps=slippage_bps,
                         initial_cash=initial_cash)
        curve = [row["equity"] for row in study["daily"]]
        results.append({"name": name, "parameters": study["parameters"],
                        "fullPeriod": study["result"],
                        "holdout": _holdout_metrics(curve, start_index, initial_cash),
                        "orderCount": study["result"]["orderCount"]})
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    test_count = len(days) - start_index
    return {"source": str(path), "sourceSha256": digest,
            "testStartRequested": test_start.isoformat(), "testStartActual": days[start_index].isoformat(),
            "testSessions": test_count, "minTestSessions": min_test_sessions,
            "status": "EXPLORATORY_HOLDOUT_ONLY" if test_count >= min_test_sessions else "NO_GO_TEST_TOO_SHORT",
            "candidates": results, "winnerSelected": False, "liveOrdersEnabled": False,
            "warning": "The holdout is valid only if these candidates and test_start were fixed before seeing it."
                       " Prices, historical universe and costs must be independently verified."}


def main() -> None:
    parser = argparse.ArgumentParser(description="Fixed US equity short-horizon research candidates")
    parser.add_argument("csv", type=Path)
    parser.add_argument("--test-start", type=date.fromisoformat, required=True)
    parser.add_argument("--top-n", type=int, default=50)
    parser.add_argument("--fee-bps", type=float, default=10)
    parser.add_argument("--slippage-bps", type=float, default=5)
    parser.add_argument("--initial-cash", type=float, default=5000)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    result = compare(args.csv, test_start=args.test_start, top_n=args.top_n,
                     fee_bps=args.fee_bps, slippage_bps=args.slippage_bps,
                     initial_cash=args.initial_cash)
    output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output, encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
