import json
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from paperdesk.engine import PaperEngine
from paperdesk.feed import DemoFeed
from paperdesk.server import make_handler


class PaperDeskTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_path = Path(self.temp.name) / "paper.json"
        self.engine = PaperEngine(self.state_path, demo=True)
        self.feed = DemoFeed()

    def live_scan(self):
        scan = self.feed.scan()
        scan["synthetic"] = False
        for quote in scan["rows"]:
            price = quote["lastPrice"]
            quote.update({"bidPrice": round(price * 0.9995, 4),
                          "askPrice": round(price * 1.0005, 4),
                          "bboValid": True, "sourceAsOf": scan["at"]})
        for quote in scan["benchmarks"].values():
            quote.update({"lastPrice": 100, "usable": True, "fresh": True,
                          "sourceAsOf": scan["at"]})
        return scan

    def test_incomplete_scan_blocks_new_positions(self):
        self.engine.control("start")
        scan = self.feed.scan()
        scan["complete"] = False
        self.engine.apply(scan)
        self.assertEqual(self.engine.state()["gate"], "INCOMPLETE_OR_STALE_MARKET_SCAN")
        self.assertFalse(self.engine.state()["account"]["positions"])
        self.assertFalse(self.engine.state()["account"]["trades"])

    def test_missing_benchmark_blocks_all_simulated_fills(self):
        self.engine.control("start")
        scan = self.feed.scan()
        scan["benchmarks"] = {}
        self.engine.apply(scan)
        self.assertEqual(self.engine.state()["gate"], "BENCHMARK_QUOTES_MISSING_OR_STALE")
        self.assertFalse(self.engine.state()["account"]["trades"])

    def test_paper_entry_regime_exit_persistence_and_paused_restart(self):
        self.engine.apply(self.feed.scan())
        self.assertFalse(self.engine.state()["account"]["trades"])
        self.engine.control("start")
        self.engine.apply(self.feed.scan())
        state = self.engine.state()
        self.assertEqual(state["gate"], "READY")
        self.assertEqual(len(state["account"]["positions"]), 3)
        self.assertTrue(all(t["simulated"] for t in state["account"]["trades"]))
        scan = self.feed.scan()
        scan["benchmarks"] = {"SPY": {"changePct": -1}, "QQQ": {"changePct": -1}}
        self.engine.apply(scan)
        self.assertFalse(self.engine.state()["account"]["positions"])
        self.assertTrue(any(t["side"] == "SELL" for t in self.engine.state()["account"]["trades"]))
        sold = {t["code"] for t in self.engine.state()["account"]["trades"] if t["side"] == "SELL"}
        scan["benchmarks"] = {"SPY": {"changePct": 1}, "QQQ": {"changePct": 1}}
        self.engine.apply(scan)
        self.assertFalse(sold & set(self.engine.state()["account"]["positions"]))
        restored = PaperEngine(self.state_path, demo=True)
        self.assertFalse(restored.running)
        self.assertEqual(restored.state()["account"]["cash"], self.engine.state()["account"]["cash"])

    def test_loopback_api_requires_token_and_same_origin_for_control(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.engine, "testtoken"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}/t/testtoken"
        with urlopen(url + "/api/state") as response:
            self.assertFalse(json.load(response)["running"])
        with self.assertRaises(HTTPError) as denied:
            urlopen(Request(url + "/api/start", data=b"", method="POST"))
        self.assertEqual(denied.exception.code, 403)
        request = Request(url + "/api/start", data=b"", method="POST",
                          headers={"Origin": f"http://127.0.0.1:{server.server_port}"})
        with urlopen(request) as response:
            self.assertTrue(json.load(response)["running"])
        with self.assertRaises(HTTPError) as missing:
            urlopen(f"http://127.0.0.1:{server.server_port}/api/state")
        self.assertEqual(missing.exception.code, 404)

    def test_real_paper_fills_use_displayed_ask_and_bid(self):
        live = PaperEngine(Path(self.temp.name) / "live.json", demo=False)
        live.control("start")
        live.apply(self.live_scan())
        self.assertEqual(live.account["trades"], [])
        self.assertEqual(len(live.account["pending"]), 3)
        scan = self.live_scan()
        live.apply(scan)
        buys = [t for t in live.account["trades"] if t["side"] == "BUY"]
        self.assertGreaterEqual(len(buys), 1)
        first = buys[0]
        quote = next(r for r in scan["rows"] if r["code"] == first["code"])
        self.assertGreaterEqual(first["price"], quote["askPrice"])
        self.assertEqual(first["fillModel"], "DISPLAYED_BBO_PLUS_SLIPPAGE")
        self.assertLess(datetime.fromisoformat(first["signalAt"]),
                        datetime.fromisoformat(first["quoteAsOf"]))
        scan = self.live_scan()
        quote = next(r for r in scan["rows"] if r["code"] == first["code"])
        quote["lastPrice"] = first["price"] * 0.9
        quote["bidPrice"] = round(quote["lastPrice"] * 0.9995, 4)
        quote["askPrice"] = round(quote["lastPrice"] * 1.0005, 4)
        live.apply(scan)
        self.assertFalse([t for t in live.account["trades"] if t["side"] == "SELL"])
        self.assertTrue(any(s["side"] == "SELL" and s["code"] == first["code"]
                            for s in live.account["pending"]))
        scan = self.live_scan()
        quote = next(r for r in scan["rows"] if r["code"] == first["code"])
        quote["lastPrice"] = first["price"] * 0.9
        quote["bidPrice"] = round(quote["lastPrice"] * 0.9995, 4)
        quote["askPrice"] = round(quote["lastPrice"] * 1.0005, 4)
        live.apply(scan)
        sells = [t for t in live.account["trades"] if t["side"] == "SELL" and t["code"] == first["code"]]
        self.assertEqual(len(sells), 1)
        self.assertLessEqual(sells[0]["price"], quote["bidPrice"])

    def test_invalid_bbo_never_creates_live_fill(self):
        live = PaperEngine(Path(self.temp.name) / "live.json", demo=False)
        live.control("start")
        scan = self.live_scan()
        for quote in scan["rows"]:
            quote["bboValid"] = False
        live.apply(scan)
        self.assertEqual(live.account["trades"], [])
        self.assertEqual(live.account["positions"], {})

    def test_real_pending_cancels_on_incomplete_scan(self):
        live = PaperEngine(Path(self.temp.name) / "live.json", demo=False)
        live.control("start")
        live.apply(self.live_scan())
        self.assertTrue(live.account["pending"])
        scan = self.live_scan()
        scan["complete"] = False
        live.apply(scan)
        self.assertEqual(live.account["pending"], [])
        live.apply(self.live_scan())
        self.assertEqual(live.account["trades"], [])
        self.assertTrue(live.account["pending"])

    def test_real_pending_cancels_on_pause_and_restart(self):
        path = Path(self.temp.name) / "live.json"
        live = PaperEngine(path, demo=False)
        live.control("start")
        live.apply(self.live_scan())
        self.assertTrue(live.account["pending"])
        restored = PaperEngine(path, demo=False)
        self.assertTrue(restored.account["pending"])
        restored.control("start")
        self.assertEqual(restored.account["pending"], [])
        restored.apply(self.live_scan())
        self.assertEqual(restored.account["trades"], [])
        self.assertTrue(restored.account["pending"])
        restored.control("pause")
        self.assertEqual(restored.account["pending"], [])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["account"]["pending"], [])

    def test_pause_stays_effective_when_state_write_fails(self):
        live = PaperEngine(Path(self.temp.name) / "live.json", demo=False)
        live.control("start")
        live.apply(self.live_scan())
        self.assertTrue(live.account["pending"])
        with patch.object(live, "persist", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                live.control("pause")
        self.assertFalse(live.running)
        self.assertEqual(live.account["pending"], [])

    def test_real_pending_requires_later_quote_source_and_five_minute_limit(self):
        live = PaperEngine(Path(self.temp.name) / "live.json", demo=False)
        live.control("start")
        first = self.live_scan()
        live.apply(first)
        scan = self.live_scan()
        for quote in scan["rows"]:
            quote["sourceAsOf"] = first["at"]
        live.apply(scan)
        self.assertEqual(live.account["trades"], [])
        old = (datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat()
        for signal in live.account["pending"]:
            signal["signalAt"] = old
        live.apply(self.live_scan())
        self.assertEqual(live.account["trades"], [])

    def test_replayed_scan_is_rejected_even_after_restart(self):
        self.engine.control("start")
        scan = self.feed.scan()
        self.engine.apply(scan)
        trade_count = len(self.engine.account["trades"])
        restored = PaperEngine(self.state_path, demo=True)
        restored.control("start")
        restored.apply(scan)
        self.assertEqual(restored.state()["gate"], "DUPLICATE_OR_OUT_OF_ORDER_SCAN")
        self.assertEqual(len(restored.account["trades"]), trade_count)
        self.assertEqual(restored.account["day"], datetime.fromisoformat(scan["at"]).astimezone(
            ZoneInfo("America/New_York")).date().isoformat())

    def test_legacy_state_migrates_trade_time_to_replay_guard(self):
        self.engine.control("start")
        scan = self.feed.scan()
        self.engine.apply(scan)
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        saved["account"].pop("lastAppliedAt")
        self.state_path.write_text(json.dumps(saved), encoding="utf-8")
        restored = PaperEngine(self.state_path, demo=True)
        restored.control("start")
        restored.apply(scan)
        self.assertEqual(restored.state()["gate"], "DUPLICATE_OR_OUT_OF_ORDER_SCAN")

    def test_live_quote_source_time_and_actual_spread_guard_entries(self):
        live = PaperEngine(Path(self.temp.name) / "live.json", demo=False)
        live.control("start")
        scan = self.live_scan()
        stale = (datetime.fromisoformat(scan["at"]) - timedelta(minutes=10)).isoformat()
        for quote in scan["rows"]:
            quote["sourceAsOf"] = stale
        live.apply(scan)
        self.assertEqual(live.account["trades"], [])
        scan = self.live_scan()
        for quote in scan["rows"]:
            quote["bidPrice"] = quote["lastPrice"] * 0.9
            quote["askPrice"] = quote["lastPrice"] * 1.1
            quote["spreadBps"] = 10  # claimed spread cannot override actual BBO
        live.apply(scan)
        self.assertEqual(live.account["trades"], [])

    def test_invalid_live_bbo_prevents_exit_fill(self):
        live = PaperEngine(Path(self.temp.name) / "live.json", demo=False)
        live.control("start")
        live.apply(self.live_scan())
        live.apply(self.live_scan())
        live.control("pause")
        live.control("start")
        before = len(live.account["trades"])
        held = set(live.account["positions"])
        scan = self.live_scan()
        for quote in scan["rows"]:
            if quote["code"] in held:
                quote["lastPrice"] *= 0.5
                quote["bboValid"] = False
        live.apply(scan)
        self.assertEqual(len(live.account["trades"]), before)
        self.assertEqual(set(live.account["positions"]), held)

    def test_live_stop_signal_sells_on_next_quote_even_after_rebound(self):
        live = PaperEngine(Path(self.temp.name) / "live.json", demo=False)
        live.control("start")
        live.apply(self.live_scan())
        live.apply(self.live_scan())
        self.assertTrue(live.account["positions"])
        code, position = next(iter(live.account["positions"].items()))
        live.control("pause")
        live.control("start")
        scan = self.live_scan()
        quote = next(r for r in scan["rows"] if r["code"] == code)
        quote["lastPrice"] = position["entry"] * 0.95
        quote["bidPrice"] = round(quote["lastPrice"] * 0.9995, 4)
        quote["askPrice"] = round(quote["lastPrice"] * 1.0005, 4)
        live.apply(scan)
        self.assertTrue(any(s["code"] == code and s["side"] == "SELL" and
                            s["reason"] == "STOP_3_PERCENT" for s in live.account["pending"]))
        self.assertIn(code, live.account["positions"])
        scan = self.live_scan()
        quote = next(r for r in scan["rows"] if r["code"] == code)
        quote["lastPrice"] = position["entry"] * 1.02
        quote["bidPrice"] = round(quote["lastPrice"] * 0.9995, 4)
        quote["askPrice"] = round(quote["lastPrice"] * 1.0005, 4)
        live.apply(scan)
        sells = [t for t in live.account["trades"] if t["code"] == code and t["side"] == "SELL"]
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["reason"], "STOP_3_PERCENT")

    def test_loss_limit_latches_for_market_day_and_survives_restart(self):
        self.engine.control("start")
        self.engine.apply(self.feed.scan())
        scan = self.feed.scan()
        held = self.engine.account["positions"]
        for quote in scan["rows"]:
            if quote["code"] in held:
                quote["lastPrice"] = held[quote["code"]]["entry"] * 0.5
        self.engine.apply(scan)
        self.assertEqual(self.engine.state()["gate"], "DAILY_LOSS_LIMIT")
        buy_count = sum(t["side"] == "BUY" for t in self.engine.account["trades"])
        restored = PaperEngine(self.state_path, demo=True)
        restored.control("start")
        restored.apply(self.feed.scan())
        self.assertEqual(restored.state()["gate"], "DAILY_LOSS_LIMIT")
        self.assertEqual(sum(t["side"] == "BUY" for t in restored.account["trades"]), buy_count)

    def test_overnight_gap_counts_against_new_day_limit(self):
        self.engine.control("start")
        prior = self.feed.scan()
        prior["at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        self.engine.apply(prior)
        prior_equity = self.engine.state()["account"]["equity"]
        held = self.engine.account["positions"]
        scan = self.feed.scan()
        for quote in scan["rows"]:
            if quote["code"] in held:
                quote["lastPrice"] = held[quote["code"]]["entry"] * 0.5
        self.engine.apply(scan)
        self.assertAlmostEqual(self.engine.account["dayStartEquity"], prior_equity, places=2)
        self.assertEqual(self.engine.state()["gate"], "DAILY_LOSS_LIMIT")

    def test_coverage_counts_cannot_claim_missing_quotes(self):
        self.engine.control("start")
        scan = self.feed.scan()
        scan["rows"] = scan["rows"][:100]
        self.engine.apply(scan)
        self.assertEqual(self.engine.state()["gate"], "INCOMPLETE_OR_STALE_MARKET_SCAN")
        self.assertFalse(self.engine.account["trades"])

    def test_invalid_persisted_account_is_rejected(self):
        self.state_path.write_text(json.dumps({"demo": True, "account": {
            "cash": float("nan"), "positions": {}, "trades": [], "cooldowns": {},
            "day": "", "dayStartEquity": 100000}}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Invalid paper account"):
            PaperEngine(self.state_path, demo=True)


if __name__ == "__main__":
    unittest.main()
