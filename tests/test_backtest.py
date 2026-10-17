import csv
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from quantlab.backtest import Bar, backtest, load_bars


class BacktestTests(unittest.TestCase):
    def setUp(self):
        start = date(2026, 1, 1)
        self.bars = [Bar(start + timedelta(days=i), price, price)
                     for i, price in enumerate([10, 12, 11, 14, 16, 13])]

    def test_signal_waits_until_next_open(self):
        bars = self.bars.copy()
        bars[2] = Bar(bars[2].day, 20, 10)
        result = backtest(bars, short=1, long=2, fee_bps=0, slippage_bps=0)
        self.assertEqual(result["equityCurve"][1]["equity"], 1000)
        self.assertEqual(result["equityCurve"][2]["equity"], 500)
        self.assertEqual(result["trades"][0]["date"], bars[2].day.isoformat())
        self.assertEqual(result["trades"][0]["fillPrice"], 20)

    def test_costs_reduce_equity(self):
        free = backtest(self.bars, short=1, long=2, fee_bps=0, slippage_bps=0)
        costly = backtest(self.bars, short=1, long=2, fee_bps=10, slippage_bps=5)
        self.assertLess(costly["result"]["endingEquity"], free["result"]["endingEquity"])

    def test_reject_missing_day_and_invalid_price(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bars.csv"
            with path.open("w", newline="") as output:
                writer = csv.writer(output)
                writer.writerows([["date", "open", "close"], ["2026-01-01", 10, 10],
                                  ["2026-01-03", 11, 11]])
            with self.assertRaisesRegex(ValueError, "missing UTC days"):
                load_bars(path)
            path.write_text("date,open,close\n2026-01-01,0,10\n")
            with self.assertRaisesRegex(ValueError, "positive"):
                load_bars(path)


if __name__ == "__main__":
    unittest.main()
