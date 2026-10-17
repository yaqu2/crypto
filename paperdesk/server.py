"""Loopback dashboard with token-scoped controls; all trades stay simulated."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import secrets
import socket
import threading
import time
import webbrowser

from .engine import PaperEngine
from .feed import DemoFeed, MoomooFeed
from .record import record_scan


LIVE_SCAN_DIR = Path("data/paperdesk/scans")
DEFAULT_EVIDENCE_REPORT = Path("data/paperdesk/eval.json")


class EvidenceReader:
    """Expose only a small, explicitly unverified summary of a local report."""

    def __init__(self, path=DEFAULT_EVIDENCE_REPORT):
        self.path = Path(path)
        self.lock = threading.Lock()
        self._version = None
        self._summary = {"status": "NOT_EVALUATED"}

    def read(self):
        with self.lock:
            try:
                stat = self.path.stat()
            except FileNotFoundError:
                self._version = None
                self._summary = {"status": "NOT_EVALUATED"}
                return self._summary
            version = (stat.st_mtime_ns, stat.st_size)
            if version == self._version:
                return self._summary
            self._version = version
            if stat.st_size > 32 * 1024 * 1024:
                self._summary = {"status": "INVALID_REPORT"}
                return self._summary
            try:
                report = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(report, dict) or report.get("status") not in ("NO_GO", "RESEARCH_ONLY"):
                    raise ValueError("unexpected evaluation status")
                sample = report.get("sample") or {}
                train = report.get("train") or {}
                holdout = report.get("holdout") or {}
                stress = report.get("holdoutStress") or {}
                if not all(isinstance(x, dict) for x in (sample, train, holdout, stress)):
                    raise ValueError("invalid evaluation summary")

                def finite(value):
                    return value if type(value) in (int, float) and math.isfinite(value) else None

                reasons = report.get("reasons")
                if report["status"] == "RESEARCH_ONLY":
                    days = finite(sample.get("regularTradingDays"))
                    holdout_days = finite(sample.get("holdoutTradingDays"))
                    holdout_trades = finite(holdout.get("completedRoundTrips"))
                    train_trades = finite(train.get("completedRoundTrips"))
                    result = finite(holdout.get("netReturnPct"))
                    spy = finite(holdout.get("spyNetReturnPct"))
                    stressed = finite(stress.get("netReturnPct"))
                    coverage = finite(sample.get("completeFreshScanFraction"))
                    full_days = finite(sample.get("fullSessionDays"))
                    if (reasons or None in (days, holdout_days, holdout_trades, train_trades,
                                            result, spy, stressed, coverage, full_days) or
                            days < 60 or holdout_days < 20 or holdout_trades < 30 or
                            train_trades + holdout_trades < 100 or coverage < .95 or
                            full_days / days < .95 or result <= 0 or result <= spy or stressed <= 0):
                        raise ValueError("research status contradicts reported gates")
                self._summary = {
                    "status": report["status"],
                    "reasons": [str(reason)[:100] for reason in reasons[:5]] if isinstance(reasons, list) else [],
                    "regularDays": finite(sample.get("regularTradingDays")),
                    "holdoutDays": finite(sample.get("holdoutTradingDays")),
                    "completedRoundTrips": finite(holdout.get("completedRoundTrips")),
                    "holdoutReturnPct": finite(holdout.get("netReturnPct")),
                    "spyReturnPct": finite(holdout.get("spyNetReturnPct")),
                    "stressReturnPct": finite(stress.get("netReturnPct")),
                    "lastAt": str(sample.get("lastAt") or "")[:40],
                }
            except (OSError, ValueError, UnicodeError, TypeError):
                self._summary = {"status": "INVALID_REPORT"}
            return self._summary


def capture_and_apply(feed, engine, scan_dir=None):
    """Archive a live observation before any paper order can be inferred."""
    try:
        scan = feed.scan()
    except Exception as exc:
        with engine.lock:
            engine.running = False
            engine.error = f"FEED_ERROR: {type(exc).__name__}: {str(exc)[:180]}"
            engine.gate = "FEED_ERROR"
            engine.opportunities = []
        return False
    if not engine.demo:
        try:
            record_scan(scan, scan_dir or LIVE_SCAN_DIR)
        except Exception as exc:
            with engine.lock:
                engine.running = False  # require a new explicit click after repair
                engine.error = f"RECORD_ERROR: {type(exc).__name__}: {str(exc)[:180]}"
                engine.gate = "RECORD_ERROR"
                engine.opportunities = []
            return False
    engine.apply(scan)
    return True


def make_handler(engine, token, evidence=None):
    prefix = f"/t/{token}"
    page = (Path(__file__).parent / "dashboard.html").read_bytes()
    evidence = evidence or EvidenceReader()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def respond(self, code, body, kind="application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == prefix + "/":
                self.respond(200, page, "text/html; charset=utf-8")
            elif self.path == prefix + "/api/state":
                state = engine.state()
                state["evidence"] = evidence.read()
                self.respond(200, json.dumps(state).encode())
            else:
                self.respond(404, b'{}')

        def do_POST(self):
            origin = self.headers.get("Origin", "")
            expected = f"http://127.0.0.1:{self.server.server_port}"
            if origin != expected or self.path not in (prefix + "/api/start", prefix + "/api/pause"):
                self.respond(403, b'{}')
                return
            if self.headers.get("Content-Length") not in ("0", None):
                self.respond(400, b'{}')
                return
            action = self.path.rsplit("/", 1)[-1]
            self.respond(200, json.dumps(engine.control(action)).encode())

    return Handler


def serve(feed, engine, port=0, browser=True, interval=None, scan_dir=None, evidence_path=DEFAULT_EVIDENCE_REPORT):
    token = secrets.token_urlsafe(24)
    interval = interval or (3 if engine.demo else 90)
    server = ThreadingHTTPServer(("127.0.0.1", port),
                                 make_handler(engine, token, EvidenceReader(evidence_path)))
    server.daemon_threads = True
    stop = threading.Event()

    def scan_loop():
        while not stop.is_set():
            try:
                capture_and_apply(feed, engine, scan_dir)
            except Exception as exc:
                with engine.lock:
                    engine.running = False
                    engine.error = f"{type(exc).__name__}: {str(exc)[:220]}"
                    engine.gate = "ENGINE_ERROR"
            stop.wait(interval)

    worker = threading.Thread(target=scan_loop, name="paperdesk-scan", daemon=True)
    worker.start()
    url = f"http://127.0.0.1:{server.server_port}/t/{token}/"
    print(f"PaperDesk: {url}", flush=True)
    print("仅本机访问；全部订单为模拟成交。Ctrl+C 结束。", flush=True)
    if browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        stop.set()
        server.server_close()
        worker.join(timeout=3)
        if hasattr(feed, "close"):
            feed.close()


def main():
    parser = argparse.ArgumentParser(description="Local US-stock paper dashboard")
    parser.add_argument("--demo", action="store_true", help="use synthetic test data")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--scan-dir", type=Path, default=LIVE_SCAN_DIR,
                        help="live OpenD scan archive directory (one gzip JSON per scan)")
    parser.add_argument("--evidence-report", type=Path, default=DEFAULT_EVIDENCE_REPORT,
                        help="optional local intraday evaluation JSON to summarize in the dashboard")
    args = parser.parse_args()
    if not args.demo:
        try:
            with socket.create_connection(("127.0.0.1", 11111), timeout=3):
                pass
        except OSError:
            parser.exit(2, "本机 OpenD 127.0.0.1:11111 不可用；请在同一台 Mac 启动并登录 OpenD。\n")
    feed = DemoFeed() if args.demo else MoomooFeed()
    state = args.state or Path("data/paperdesk/demo.json" if args.demo else "data/paperdesk/live.json")
    engine = PaperEngine(state, demo=args.demo)
    serve(feed, engine, args.port, not args.no_browser,
          scan_dir=args.scan_dir, evidence_path=args.evidence_report)


if __name__ == "__main__":
    main()
