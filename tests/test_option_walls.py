import unittest

from quantlab.option_walls import summarize
from tools.moomoo_option_snapshot import collect


class Frame:
    def __init__(self, rows):
        self.rows = rows

    def to_dict(self, mode):
        assert mode == "records"
        return self.rows

    def __len__(self):
        return len(self.rows)


class FakeOpenD:
    def get_option_chain(self, underlying, *, start, end):
        return 0, Frame([
            {"code": "US.C1", "option_type": "CALL", "strike_time": end,
             "strike_price": 101},
            {"code": "US.P1", "option_type": "PUT", "strike_time": end,
             "strike_price": 99},
        ])

    def get_market_snapshot(self, codes):
        if codes == ["US.SPY"]:
            return 0, Frame([{"code": "US.SPY", "last_price": 100,
                              "data_date": "2026-10-17", "data_time": "12:00:00"}])
        return 0, Frame([
            {"code": "US.C1", "open_interest": 100, "contract_size": 100,
             "data_date": "2026-10-17", "data_time": "12:00:00"},
            {"code": "US.P1", "open_interest": 200, "contract_size": 100,
             "data_date": "2026-10-17", "data_time": "12:00:00"},
        ])


class OptionWallsTests(unittest.TestCase):
    def test_snapshot_reports_levels_without_direction(self):
        report = collect(FakeOpenD(), "US.SPY", "2026-10-17", "2026-10-24")
        self.assertEqual(report["summary"]["status"], "OBSERVATIONAL_OI_LEVELS_ONLY")
        self.assertEqual(report["summary"]["directionalForecast"], None)
        self.assertEqual(report["summary"]["groups"][1]["topLevels"][0]["shareEquivalentOpenInterest"], 20000)
        self.assertFalse(report["historicalPointInTimeSeries"])

    def test_missing_oi_is_not_zero_filled(self):
        report = summarize([{"optionType": "CALL", "expiry": "2026-10-24",
                             "strike": 100, "openInterest": None, "contractSize": 100}], 100)
        self.assertEqual(report["status"], "NO_GO_NO_USABLE_OPEN_INTEREST")
        self.assertEqual(report["excludedRows"]["INVALID_NUMBER"], 1)


if __name__ == "__main__":
    unittest.main()
