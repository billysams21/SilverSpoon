"""Headless checks for the web API. Run: python test_webapp.py

Drives the FastAPI app with its test client against an engine whose worker
threads are off, so nothing is downloaded.
"""
import os
import tempfile

DATA = tempfile.mkdtemp(prefix="silverspoon-test-web-")
os.environ["SILVERSPOON_DATA_DIR"] = DATA

from fastapi.testclient import TestClient

import engine as E
from webapp.server import create_app


def client():
    for name in (".silverspoon_settings.json", ".silverspoon_history.json"):
        path = os.path.join(DATA, name)
        if os.path.exists(path):
            os.remove(path)
    eng = E.DownloadEngine(autostart=False)
    return TestClient(create_app(eng)), eng


def test_static_ui_is_served():
    with client()[0] as c:
        assert "SilverSpoon" in c.get("/").text
        assert c.get("/app.js").status_code == 200
        assert c.get("/style.css").status_code == 200


def test_parse_and_add_and_state():
    c, eng = client()
    with c:
        parsed = c.post("/api/links/parse", json={"text": "https://h.example/Game.part1.rar\njunk"}).json()
        assert parsed["links"] == ["https://h.example/Game.part1.rar"]
        assert parsed["suggested_folder"] == "Game"

        r = c.post("/api/tasks", json={"links": parsed["links"], "folder_name": "Game", "save_dir": DATA})
        assert r.status_code == 200 and len(r.json()["added"]) == 1
        state = c.get("/api/state").json()
        assert state["tasks"][0]["status"] == "Queued" and state["batches"][0]["name"] == "Game"


def test_add_rejects_bad_input():
    c, _ = client()
    with c:
        assert c.post("/api/tasks", json={"links": ["nope"], "folder_name": "x"}).status_code == 400
        assert c.post("/api/tasks", json={"links": ["https://h.example/a.zip"], "folder_name": " "}).status_code == 400
        for bad in ("../escape", "a/b", ".."):
            assert c.post("/api/tasks", json={"links": ["https://h.example/a.zip"], "folder_name": bad}).status_code == 400


def test_task_actions():
    c, eng = client()
    with c:
        uid = c.post("/api/tasks", json={"links": ["https://h.example/a.zip"], "folder_name": "A",
                                          "save_dir": DATA}).json()["added"][0]
        c.post("/api/tasks/start", json={"uids": [uid]})
        assert eng.tasks[0].status == "Pending"
        c.post("/api/tasks/pause", json={"uids": [uid]})
        assert eng.tasks[0].status == "Paused"
        c.post("/api/tasks/cancel", json={"uids": [uid]})
        assert eng.tasks[0].status == "Cancelled"
        assert c.post("/api/tasks/bogus", json={"uids": [uid]}).status_code == 404
        assert c.post("/api/tasks/clear-finished", json={}).json() == {"removed": 1}
        assert eng.tasks == []


def test_delete_and_redownload_confirmation():
    c, eng = client()
    with c:
        uid = c.post("/api/tasks", json={"links": ["https://h.example/a.zip"], "folder_name": "A",
                                          "save_dir": DATA}).json()["added"][0]
        task = eng.tasks[0]
        os.makedirs(task.save_dir, exist_ok=True)
        open(task.filepath, "wb").write(b"partial")
        task.status, task.progress = "Paused", 40.0
        # Partial data would be lost: the server asks first and touches nothing.
        r = c.post("/api/tasks/redownload", json={"uids": [uid]}).json()
        assert r == {"confirm": ["a.zip"]} and os.path.exists(task.filepath)
        r = c.post("/api/tasks/redownload?confirm=true", json={"uids": [uid]}).json()
        assert r["queued"] == 1 and not os.path.exists(task.filepath) and task.status == "Pending"

        open(task.filepath, "wb").write(b"x")
        assert c.post("/api/tasks/delete", json={"uids": [uid], "delete_files": True}).json() == {"removed": 1}
        assert not os.path.exists(task.filepath) and eng.tasks == []


def test_settings_validation_and_persistence():
    c, eng = client()
    with c:
        s = c.put("/api/settings", json={"max_workers": 5, "extract_after_download": True,
                                          "unknown_key": 1}).json()
        assert s["max_workers"] == 5 and s["extract_after_download"] is True and "unknown_key" not in s
        assert eng.settings["max_workers"] == 5
        for bad in ({"max_workers": 99}, {"max_workers": "3"}, {"theme": "Neon"},
                    {"extract_after_download": "yes"}, {"captcha_timeout": 1}):
            assert c.put("/api/settings", json=bad).status_code == 400, bad
        assert c.get("/api/settings").json()["max_workers"] == 5


def test_schedule_validation_and_targets():
    c, eng = client()
    with c:
        uid = c.post("/api/tasks", json={"links": ["https://h.example/a.zip"], "folder_name": "A",
                                          "save_dir": DATA}).json()["added"][0]
        ok = {"enabled": True, "start": "23:00", "end": "05:00", "recurrence": "weekly", "days": [0, 2, 4]}
        r = c.put("/api/schedule", json={**ok, "uids": [uid]})
        assert r.status_code == 200 and r.json()["targets"] == [uid]
        assert "Mon, Wed, Fri" in r.json()["description"]
        assert c.get("/api/state").json()["schedule"]["config"]["enabled"] is True

        assert c.put("/api/schedule", json={**ok, "days": []}).status_code == 400
        assert c.put("/api/schedule", json={**ok, "start": "25:99"}).status_code == 400
        assert c.put("/api/schedule", json={**ok, "recurrence": "once", "date": "2001-01-01"}).status_code == 400
        assert c.put("/api/schedule", json={**ok, "recurrence": "once", "date": "nope"}).status_code == 400
        # Removing a schedule needs no valid days.
        assert c.put("/api/schedule", json={**ok, "enabled": False, "days": []}).status_code == 200
        assert c.get("/api/schedule").json()["description"] == ""


def test_captcha_flag_and_websocket_push():
    c, eng = client()
    with c:
        eng.add_links(["https://h.example/a.zip"], DATA, "A")
        assert c.get("/api/captcha").json()["waiting"] is False
        eng.tasks[0].status = E.CAPTCHA_WAIT_STATUS
        assert c.get("/api/captcha").json()["waiting"] is True
        with c.websocket_connect("/api/ws") as ws:
            msg = ws.receive_json()
        assert msg["stats"]["captcha_waiting"] is True and msg["tasks"][0]["filename"] == "a.zip"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
