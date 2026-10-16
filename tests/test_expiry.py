import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


def test_passive_expiry_on_read(d, conn):
    d(conn, "SET", "k", "v", "PX", "10")
    time.sleep(0.05)
    assert d(conn, "GET", "k") is None  # lazily purged on access
    assert d(conn, "EXISTS", "k") == 0


def test_active_expiry_sweeps_untouched_keys(engine, d, conn):
    d(conn, "SET", "k", "v", "PX", "10")
    time.sleep(0.05)
    swept = engine.expirer.run_once()
    assert swept >= 1
    # confirm it's really gone from the dict, not just reported expired
    assert "k" not in engine.databases[0].data


def test_expiry_emits_cdc_event(engine, d, conn):
    d(conn, "SET", "k", "v", "PX", "10")
    time.sleep(0.05)
    engine.expirer.run_once()
    events = engine.cdc.since(0)
    assert any(e.event_type == "expired" and e.key == "k" for e in events)


def test_expiry_fires_registered_webhook(engine, d, conn):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            received.append(json.loads(self.rfile.read(length)))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        engine.hooks.register("test_hook", 0, "session:*", f"http://127.0.0.1:{port}/hook")
        d(conn, "SET", "session:1", "v", "PX", "10")
        time.sleep(0.05)
        engine.expirer.run_once()

        deadline = time.time() + 2
        while not received and time.time() < deadline:
            time.sleep(0.02)
        assert received and received[0]["key"] == "session:1"
    finally:
        server.shutdown()


def test_persist_removes_ttl(d, conn):
    d(conn, "SET", "k", "v", "EX", "100")
    d(conn, "PERSIST", "k")
    time.sleep(0.01)
    assert d(conn, "EXISTS", "k") == 1
    assert d(conn, "TTL", "k") == -1
