"""Loopback dashboard with token-scoped controls; all trades stay simulated."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
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


def make_handler(engine, token):
    prefix = f"/t/{token}"
    page = (Path(__file__).parent / "dashboard.html").read_bytes()

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
                self.respond(200, json.dumps(engine.state()).encode())
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


def serve(feed, engine, port=0, browser=True, interval=None, scan_dir=None):
    token = secrets.token_urlsafe(24)
    interval = interval or (3 if engine.demo else 90)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(engine, token))
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
    serve(feed, engine, args.port, not args.no_browser, scan_dir=args.scan_dir)


if __name__ == "__main__":
    main()
