"""Command-line entry point for the offline study."""

import argparse
import json
from pathlib import Path

from .backtest import run


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline crypto daily-bar moving-average backtest")
    parser.add_argument("csv", type=Path, help="UTC daily CSV: date,open,close")
    parser.add_argument("--short", type=int, default=10)
    parser.add_argument("--long", type=int, default=30)
    parser.add_argument("--fee-bps", type=float, default=10)
    parser.add_argument("--slippage-bps", type=float, default=5)
    parser.add_argument("--initial-cash", type=float, default=1000)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    result = run(args.csv, short=args.short, long=args.long, fee_bps=args.fee_bps,
                 slippage_bps=args.slippage_bps, initial_cash=args.initial_cash)
    output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output, encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
