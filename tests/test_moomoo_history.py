import tempfile
import unittest
from pathlib import Path

from tools.moomoo_history import export_rows, save


class Frame:
    def __init__(self, rows):
        self.rows = rows

    def to_dict(self, mode):
        assert mode == "records"
        return self.rows


class FakeOpenD:
    def __init__(self, rehab=None):
        self.rehab = rehab if rehab is not None else [
            {"ex_div_date": "2026-01-06", "split_ratio": 2,
             "per_cash_div": 0.25, "special_dividend": 0}
        ]

    def request_history_kline(self, code, *, autype, page_req_key, **kwargs):
        if page_req_key is None:
            return 0, Frame([{"time_key": "2026-01-05 00:00:00", "open": 100, "close": 100}]), "page2"
        price = 50 if autype == "None" else 100
        return 0, Frame([{"time_key": "2026-01-06 00:00:00", "open": price, "close": price}]), None

    def get_rehab(self, code):
        return 0, Frame(self.rehab)


class MoomooExportTests(unittest.TestCase):
    def test_paginates_and_keeps_raw_price_and_actions(self):
        rows = export_rows(FakeOpenD(), ["US.AAA"], "2026-01-05", "2026-01-06")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["symbol"], "AAA")
        self.assertEqual(rows[1]["open"], 50)
        self.assertEqual(rows[1]["adj_close"], 100)
        self.assertEqual(rows[1]["split_factor"], 2)
        self.assertEqual(rows[1]["dividend"], 0.25)

    def test_unsupported_action_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "unsupported corporate action"):
            export_rows(FakeOpenD(rehab=[{"ex_div_date": "2026-01-06", "allotment_ratio": 1}]),
                        ["US.AAA"], "2026-01-05", "2026-01-06")

    def test_output_is_hashed_and_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "history.csv"
            save([{"date": "2026-01-05", "symbol": "AAA", "open": 100, "close": 100,
                   "adj_close": 100, "split_factor": 1, "dividend": 0}], out, {"source": "test"})
            self.assertTrue(out.exists())
            self.assertIn("csvSha256", out.with_suffix(".csv.json").read_text())
            with self.assertRaises(FileExistsError):
                save([], out, {})


if __name__ == "__main__":
    unittest.main()
