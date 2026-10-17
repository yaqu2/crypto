import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
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


if __name__ == "__main__":
    unittest.main()
