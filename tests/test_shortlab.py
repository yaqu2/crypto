import csv
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from quantlab.shortlab import compare


class ShortLabTests(unittest.TestCase):
    def test_fixed_candidates_and_holdout_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic.csv"
            day = date(2026, 1, 5)
            days = []
            with path.open("w", newline="") as source:
                writer = csv.writer(source)
                writer.writerow(["date", "symbol", "open", "close", "adj_close",
                                 "split_factor", "dividend", "eligible"])
                for i in range(130):
                    while day.weekday() >= 5:
                        day += timedelta(days=1)
                    days.append(day)
                    for symbol, price, eligible in (("AAA", 100 + i * 0.3, 1),
                                                     ("BBB", 100 + i * 0.1, 1),
                                                     ("SPY", 200 + i * 0.15, 0),
                                                     ("QQQ", 300 + i * 0.2, 0)):
                        writer.writerow([day, symbol, price, price, price, 1, 0, eligible])
                    day += timedelta(days=1)
            result = compare(path, test_start=days[65], top_n=2, min_test_sessions=60)
            self.assertEqual(result["status"], "EXPLORATORY_HOLDOUT_ONLY")
            self.assertEqual(len(result["candidates"]), 9)
            self.assertFalse(result["winnerSelected"])
            short = compare(path, test_start=days[100], top_n=2, min_test_sessions=60)
            self.assertEqual(short["status"], "NO_GO_TEST_TOO_SHORT")
            with self.assertRaisesRegex(ValueError, "warmup"):
                compare(path, test_start=days[30], top_n=2)


if __name__ == "__main__":
    unittest.main()
