"""Headless checks for multi-connection downloads. Run: python test_segmented.py

Uses a local HTTP server that supports Range and can be made slow per
connection, throttling (503 above N connections), range-ignoring or 404.
"""
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import segmented as S

DATA = os.urandom(16 * 1024 * 1024)


def serve(per_conn_rate=0, max_conns=0, ignore_range=False, status=None):
    """Start a server for DATA; returns (url, stats, server)."""
    stats = {"served": 0, "active": 0, "peak": 0, "rejected": 0}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            if status:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            with lock:
                if max_conns and stats["active"] >= max_conns:
                    stats["rejected"] += 1
                    reject = True
                else:
                    stats["active"] += 1
                    stats["peak"] = max(stats["peak"], stats["active"])
                    reject = False
            if reject:
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            try:
                rng = self.headers.get("Range")
                start, end = 0, len(DATA) - 1
                if rng and not ignore_range:
                    a, b = rng.split("=")[1].split("-")
                    start, end = int(a), (int(b) if b else len(DATA) - 1)
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(DATA)}")
                else:
                    self.send_response(200)
                body = DATA[start:end + 1]
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                step = 64 * 1024
                for i in range(0, len(body), step):
                    part = body[i:i + step]
                    self.wfile.write(part)
                    with lock:
                        stats["served"] += len(part)
                    if per_conn_rate:
                        time.sleep(len(part) / per_conn_rate)
            except (ConnectionError, OSError):
                pass  # client closed early (pause/split): fine
            finally:
                with lock:
                    stats["active"] -= 1

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}/file.bin", stats, server


def _download(url, path, **kw):
    return S.SegmentedDownload(url, path, len(DATA), **kw).run()


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def test_probe():
    url, _, srv = serve()
    try:
        assert S.probe(url) == (len(DATA), True)
    finally:
        srv.shutdown()
    url, _, srv = serve(ignore_range=True)
    try:
        assert S.probe(url) == (len(DATA), False)
    finally:
        srv.shutdown()


def test_parallel_is_faster_and_identical():
    with tempfile.TemporaryDirectory() as d:
        times = {}
        for conns in (1, 4):
            url, stats, srv = serve(per_conn_rate=4 * 1024 * 1024)  # 4 MB/s per connection
            path = os.path.join(d, f"f{conns}.bin")
            t0 = time.time()
            try:
                assert _download(url, path, connections=conns) is True
            finally:
                srv.shutdown()
            times[conns] = time.time() - t0
            assert _read(path) == DATA
            assert not os.path.exists(S.state_path(path))
            assert stats["peak"] <= conns + 1, stats  # +1: the probe-less run never probes
            if conns == 4:
                assert stats["peak"] >= 3, stats
        assert times[4] < times[1] * 0.6, times  # ~3x faster normally; slack for a busy machine
        print(f"    1 connection {times[1]:.1f}s, 4 connections {times[4]:.1f}s")


def test_pause_and_resume():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "f.bin")
        url, stats, srv = serve(per_conn_rate=4 * 1024 * 1024)
        try:
            seen = []
            stop = lambda: bool(seen) and seen[-1] > 6 * 1024 * 1024
            assert _download(url, path, connections=4, should_stop=stop, on_progress=seen.append) is False
            assert os.path.exists(S.state_path(path))
            paused_at = seen[-1]
            assert 0 < paused_at < len(DATA)
            served_before = stats["served"]
            assert _download(url, path, connections=4) is True
        finally:
            srv.shutdown()
        assert _read(path) == DATA
        assert not os.path.exists(S.state_path(path))
        resumed = stats["served"] - served_before
        # Only the missing bytes. Slack covers data the server had pushed into
        # socket buffers at the pause; a restart would cost 6+ MB more.
        assert resumed <= len(DATA) - paused_at + 2 * 1024 * 1024, (resumed, len(DATA) - paused_at)


def test_resume_from_single_connection_partial():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "f.bin")
        with open(path, "wb") as f:
            f.write(DATA[:5 * 1024 * 1024])  # what the old one-connection path leaves
        url, stats, srv = serve()
        try:
            assert _download(url, path, connections=4) is True
        finally:
            srv.shutdown()
        assert _read(path) == DATA
        # The first 5 MB weren't fetched again. ("served" also counts bytes the
        # unthrottled server pushed into socket buffers that were never read
        # after a split, hence the slack.)
        assert stats["served"] <= len(DATA) - 5 * 1024 * 1024 + 2 * 1024 * 1024, stats


def test_throttling_host_gets_fewer_connections():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "f.bin")
        url, stats, srv = serve(per_conn_rate=8 * 1024 * 1024, max_conns=2)
        try:
            assert _download(url, path, connections=6) is True
        finally:
            srv.shutdown()
        assert _read(path) == DATA
        assert stats["rejected"] > 0 and stats["peak"] <= 2, stats


def test_range_ignored_and_dead_links_fail_fast():
    with tempfile.TemporaryDirectory() as d:
        for kwargs, exc in (({"ignore_range": True}, S.RangeNotSupported), ({"status": 404}, Exception)):
            url, _, srv = serve(**kwargs)
            t0 = time.time()
            try:
                _download(url, os.path.join(d, "f.bin"), connections=4)
            except exc as e:
                assert time.time() - t0 < 5, (kwargs, time.time() - t0)
                assert not isinstance(e, AssertionError)
            else:
                raise AssertionError(f"no error for {kwargs}")
            finally:
                srv.shutdown()


def test_bandwidth_limit():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "f.bin")
        url, _, srv = serve()
        t0 = time.time()
        try:
            assert _download(url, path, connections=4, bytes_per_second=8 * 1024 * 1024) is True
        finally:
            srv.shutdown()
        elapsed = time.time() - t0
        assert elapsed >= 1.6, elapsed  # 16 MB at 8 MB/s ~ 2 s
        assert _read(path) == DATA


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
