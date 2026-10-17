import csv
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from quantlab.backtest import Bar, backtest, load_bars


class MomentumTests(unittest.TestCase):
    def setUp(self):
        self.days = [date(2026, 1, 5) + timedelta(days=i) for i in range(4)]

    def bar(self, i, symbol, open_, close, adj=None, split=1, dividend=0, eligible=True):
        return Bar(self.days[i], symbol, open_, close, adj or close, split, dividend, eligible)

    def test_yesterdays_ranking_drives_todays_order(self):
        data = {
            self.days[0]: {"AAA": self.bar(0, "AAA", 10, 10), "BBB": self.bar(0, "BBB", 10, 10)},
            self.days[1]: {"AAA": self.bar(1, "AAA", 12, 12), "BBB": self.bar(1, "BBB", 11, 11)},
            self.days[2]: {"AAA": self.bar(2, "AAA", 20, 10), "BBB": self.bar(2, "BBB", 11, 100)},
            self.days[3]: {"AAA": self.bar(3, "AAA", 10, 10), "BBB": self.bar(3, "BBB", 100, 100)},
        }
        result = backtest(self.days, data, lookback=1, top_n=1, fee_bps=0, slippage_bps=0, initial_cash=1000)
        self.assertEqual(result["daily"][2]["selected"], ["AAA"])
        self.assertEqual(result["trades"][0]["date"], self.days[2].isoformat())
        self.assertEqual(result["trades"][0]["fillPrice"], 20)
        self.assertEqual(result["daily"][2]["equity"], 500)

    def test_split_does_not_create_a_gain(self):
        data = {day: {"AAA": bar} for day, bar in zip(self.days, [
            self.bar(0, "AAA", 100, 100, adj=50),
            self.bar(1, "AAA", 110, 110, adj=55),
            self.bar(2, "AAA", 110, 110, adj=55),
            self.bar(3, "AAA", 55, 55, adj=55, split=2),
        ])}
        result = backtest(self.days, data, lookback=1, top_n=1, fee_bps=0, slippage_bps=0, initial_cash=1000)
        self.assertEqual(result["result"]["endingEquity"], 1000)

    def test_historical_eligibility_controls_selection(self):
        data = {
            self.days[0]: {"AAA": self.bar(0, "AAA", 10, 10), "BBB": self.bar(0, "BBB", 10, 10)},
            self.days[1]: {"AAA": self.bar(1, "AAA", 20, 20, eligible=False),
                           "BBB": self.bar(1, "BBB", 11, 11)},
            self.days[2]: {"AAA": self.bar(2, "AAA", 20, 20), "BBB": self.bar(2, "BBB", 11, 11)},
            self.days[3]: {"AAA": self.bar(3, "AAA", 20, 20), "BBB": self.bar(3, "BBB", 11, 11)},
        }
        result = backtest(self.days, data, lookback=1, top_n=1, fee_bps=0, slippage_bps=0)
        self.assertEqual(result["daily"][2]["selected"], ["BBB"])

    def test_costs_reduce_result_and_missing_held_bar_fails(self):
        data = {day: {"AAA": self.bar(i, "AAA", 100 + i, 100 + i)} for i, day in enumerate(self.days)}
        free = backtest(self.days, data, lookback=1, top_n=1, fee_bps=0, slippage_bps=0)
        costly = backtest(self.days, data, lookback=1, top_n=1, fee_bps=10, slippage_bps=5)
        self.assertLess(costly["result"]["endingEquity"], free["result"]["endingEquity"])
        del data[self.days[3]]["AAA"]
        with self.assertRaisesRegex(ValueError, "missing current raw bar"):
            backtest(self.days, data, lookback=1, top_n=1)

    def test_csv_requires_historical_eligibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bars.csv"
            path.write_text("date,symbol,open,close,adj_close,split_factor,dividend\n2026-01-05,AAA,10,10,10,1,0\n")
            with self.assertRaisesRegex(ValueError, "CSV requires"):
                load_bars(path)

    def test_incomplete_universe_cannot_be_called_top_50(self):
        data = {day: {"AAA": self.bar(i, "AAA", 100 + i, 100 + i)} for i, day in enumerate(self.days)}
        with self.assertRaisesRegex(ValueError, "below top_n"):
            backtest(self.days, data, lookback=1, top_n=50)


if __name__ == "__main__":
    unittest.main()
