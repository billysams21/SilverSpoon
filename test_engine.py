"""Headless checks for the Qt-free download engine. Run: python test_engine.py

Needs curl_cffi and nodriver (requirements.txt) but no Qt and no browser.
"""
import json
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# State files must go to a scratch dir, never the real home directory.
DATA = tempfile.mkdtemp(prefix="silverspoon-test-data-")
os.environ["SILVERSPOON_DATA_DIR"] = DATA

import engine as E

PAYLOAD = os.urandom(300 * 1024)


def new_engine(**kw):
    return E.DownloadEngine(autostart=False, **kw)


def reset_state():
    for name in (".silverspoon_settings.json", ".silverspoon_history.json"):
        path = os.path.join(DATA, name)
        if os.path.exists(path):
            os.remove(path)


def serve():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            self.wfile.write(PAYLOAD)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}", server


def test_parse_links_plain_and_html():
    text = "- https://a.example/x.rar\nnot a link\n  https://b.example/y.rar  "
    assert E.parse_links(text) == ["https://a.example/x.rar", "https://b.example/y.rar"]
    html = '<table><td><a href="https://c.example/z.part1.rar">z</a></td><td><a href="/rel">r</a></td></table>'
    assert E.parse_links("ignored", html) == ["https://c.example/z.part1.rar"]
    # HTML with no usable hrefs falls back to the plain text.
    assert E.parse_links("https://d.example/f.zip", "<p>nothing</p>") == ["https://d.example/f.zip"]


def test_suggest_folder():
    assert E.suggest_folder([]) == ""
    assert E.suggest_folder(["https://h.example/Cool_Game--_fitgirl-repacks.site_--_.part01.rar"]) \
        .startswith("Cool Game")
    assert E.suggest_folder(["https://h.example/movie.mkv"]) == "movie"


def test_settings_round_trip_uses_data_dir():
    reset_state()
    eng = new_engine()
    eng.update_settings({"max_workers": 7, "captcha_timeout": 33, "show_solver_browser": True})
    assert eng.turnstile_solver.TOKEN_TIMEOUT == 33 and eng.turnstile_solver.show_window is True
    on_disk = json.load(open(os.path.join(DATA, ".silverspoon_settings.json")))
    assert on_disk["max_workers"] == 7
    assert E.load_settings()["captcha_timeout"] == 33


def test_status_transitions():
    reset_state()
    eng = new_engine()
    t1, t2 = eng.add_links(["https://h.example/a.rar", "https://h.example/b.rar"], DATA, "Batch")
    assert t1.status == "Queued"
    eng.start_tasks([t1.uid])
    assert t1.status == "Pending" and t2.status == "Queued"
    eng.pause_tasks([t1.uid])
    assert t1.status == "Paused" and t1.pause_flag
    eng.start_tasks([t1.uid])
    assert t1.status == "Pending" and not t1.pause_flag
    eng.cancel_tasks([t1.uid, t2.uid])
    assert t1.status == "Cancelled" and t2.status == "Cancelled"
    t1.status = "Error"
    t1.retry_count = 2
    eng.retry_tasks([t1.uid])
    assert t1.status == "Pending" and t1.retry_count == 0
    assert eng.clear_finished() == [t2]
    assert eng.tasks == [t1]
    eng.delete_tasks([t1.uid])
    assert eng.tasks == []


def test_snapshot_batch_aggregation():
    reset_state()
    eng = new_engine()
    a, b, c = eng.add_links(
        ["https://h.example/g.part1.rar", "https://h.example/g.part2.rar", "https://h.example/g.part3.rar"],
        DATA, "G")
    a.status, a.total_bytes, a.downloaded_bytes, a.speed = "Downloading", 100 * 2**20, 50 * 2**20, 2.0
    b.status, b.total_bytes, b.downloaded_bytes = "Completed", 100 * 2**20, 100 * 2**20
    snap = eng.snapshot()
    assert len(snap["tasks"]) == 3 and len(snap["batches"]) == 1
    batch = snap["batches"][0]
    # c has no size yet, so it's estimated from the largest known part.
    assert batch["total_bytes"] == 300 * 2**20
    assert batch["downloaded_bytes"] == 150 * 2**20
    assert batch["status"] == "Active" and batch["speed"] == 2.0
    # remaining: 50 MB of a, 0 of b, and c estimated at 100 MB, at 2 MB/s
    assert batch["eta"] == 150 / 2.0
    assert snap["stats"]["active"] == 1 and snap["stats"]["done"] == 1 and snap["stats"]["queued"] == 1
    assert snap["stats"]["speed"] == 2.0 and snap["stats"]["captcha_waiting"] is False
    c.status = E.CAPTCHA_WAIT_STATUS
    assert eng.snapshot()["stats"]["captcha_waiting"] is True


def test_history_persists_and_active_tasks_come_back_paused():
    reset_state()
    eng = new_engine()
    t, = eng.add_links(["https://h.example/p.rar"], DATA, "P")
    t.status = "Downloading"
    t.downloaded_bytes = 123
    eng.save_now()
    again = new_engine()
    assert [x.uid for x in again.tasks] == [t.uid]
    assert again.tasks[0].status == "Paused" and again.tasks[0].downloaded_bytes == 123


def test_scheduled_tasks_target_selection():
    reset_state()
    eng = new_engine()
    a, b = eng.add_links(["https://h.example/a.zip", "https://h.example/b.zip"], DATA, "S")
    assert eng._scheduled_tasks() == [a, b]
    eng.set_schedule({"enabled": True, "start": "01:00", "end": "02:00", "recurrence": "weekly",
                      "days": [0], "date": "", "keep_awake": False}, [b.uid])
    assert eng._scheduled_tasks() == [b]
    assert eng.snapshot()["schedule"]["targets"] == [b.uid]
    assert any(e["type"] == "schedule_changed" for e in eng.events)


def test_direct_download_end_to_end():
    reset_state()
    url, server = serve()
    save = tempfile.mkdtemp(prefix="silverspoon-test-dl-")
    eng = E.DownloadEngine(autostart=True)
    try:
        t, = eng.add_links([f"{url}/sample.bin"], save, "Sample")
        eng.start_tasks([t.uid])
        deadline = time.time() + 30
        while t.status != "Completed" and time.time() < deadline:
            time.sleep(0.2)
        assert t.status == "Completed", (t.status, t.error_message)
        assert open(t.filepath, "rb").read() == PAYLOAD
        assert t.progress == 100
    finally:
        eng.stop()
        server.shutdown()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
