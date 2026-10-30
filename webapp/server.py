"""FastAPI front-end for the download engine: REST + a WebSocket that pushes
the queue state, and the static single-page UI."""
import asyncio
import contextlib
import datetime as dt
import logging
import os
import sys
from typing import List, Optional

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import engine as eng
import scheduler as offpeak

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
PUSH_INTERVAL = 0.5

SETTING_TYPES = {
    "theme": str,
    "default_save_dir": str,
    "max_workers": int,
    "max_connections": int,
    "bandwidth_limit": int,
    "captcha_timeout": int,
    "show_solver_browser": bool,
    "extract_after_download": bool,
    "auto_retry_errors": bool,
    "skip_delete_confirmation": bool,
    "show_warning_dialog": bool,
}
SETTING_RANGES = {
    "max_workers": (1, 10),
    "max_connections": (1, 16),
    "bandwidth_limit": (0, 1000),
    "captcha_timeout": (5, 120),
}


def setup_logging():
    logging.basicConfig(
        level=logging.WARNING, stream=sys.stderr,
        format='%(asctime)s - %(levelname)s - %(message)s')


def create_app(engine=None):
    """``engine`` lets tests inject a DownloadEngine(autostart=False)."""
    owns_engine = engine is None

    @contextlib.asynccontextmanager
    async def lifespan(app):
        if owns_engine:
            setup_logging()
            app.state.engine = eng.DownloadEngine()
        else:
            app.state.engine = engine
        yield
        if owns_engine:
            await asyncio.to_thread(app.state.engine.stop)

    app = FastAPI(title="SilverSpoon", lifespan=lifespan)

    def get_engine() -> eng.DownloadEngine:
        return app.state.engine

    # ---- models -------------------------------------------------------
    class AddLinks(BaseModel):
        links: List[str]
        folder_name: str
        save_dir: Optional[str] = None

    class ParseLinks(BaseModel):
        text: str = ""
        html: Optional[str] = None

    class Uids(BaseModel):
        uids: List[str] = []

    class DeleteReq(Uids):
        delete_files: bool = False

    class ExtractReq(Uids):
        confirm: bool = False
        skip_folders: List[str] = []

    class ScheduleReq(BaseModel):
        enabled: bool = True
        start: str = "02:00"
        end: str = "06:00"
        recurrence: str = "weekly"
        days: List[int] = list(range(7))
        date: str = ""
        keep_awake: bool = True
        uids: Optional[List[str]] = None

    # ---- state --------------------------------------------------------
    @app.get("/api/state")
    def state():
        return get_engine().snapshot()

    @app.websocket("/api/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        try:
            while True:
                await websocket.send_json(await asyncio.to_thread(get_engine().snapshot))
                await asyncio.sleep(PUSH_INTERVAL)
        except (WebSocketDisconnect, RuntimeError):
            pass

    # ---- queue --------------------------------------------------------
    @app.post("/api/links/parse")
    def parse(req: ParseLinks):
        links = eng.parse_links(req.text, req.html)
        return {"links": links, "suggested_folder": eng.suggest_folder(links)}

    @app.post("/api/tasks")
    def add(req: AddLinks):
        links = eng.parse_links("\n".join(req.links))
        folder = req.folder_name.strip()
        if not links:
            raise HTTPException(400, "No http(s) links given")
        if not folder:
            raise HTTPException(400, "A batch folder name is required")
        # A folder name must stay inside the save directory.
        if os.path.basename(folder) != folder or folder in (".", ".."):
            raise HTTPException(400, "Invalid folder name")
        tasks = get_engine().add_links(links, req.save_dir, folder)
        return {"added": [t.uid for t in tasks]}

    simple_actions = {
        "start": "start_tasks",
        "pause": "pause_tasks",
        "cancel": "cancel_tasks",
        "retry": "retry_tasks",
    }

    @app.post("/api/tasks/clear-finished")
    def clear_finished():
        return {"removed": len(get_engine().clear_finished())}

    @app.post("/api/tasks/delete")
    def delete(req: DeleteReq):
        return {"removed": len(get_engine().delete_tasks(req.uids, req.delete_files))}

    @app.post("/api/tasks/redownload")
    def redownload(req: Uids, confirm: bool = False):
        engine = get_engine()
        with_progress = engine.redownload_with_progress(req.uids)
        if with_progress and not confirm:
            return {"confirm": [t.filename for t in with_progress]}
        return engine.force_redownload(req.uids)

    @app.post("/api/tasks/extract")
    def extract(req: ExtractReq):
        engine = get_engine()
        if not req.confirm:
            already = engine.extract_preview(req.uids)
            if already:
                return {"confirm": already}
        return {"started": engine.extract(req.uids, req.skip_folders)}

    @app.post("/api/tasks/{action}")
    def task_action(action: str, req: Uids):
        method = simple_actions.get(action)
        if not method:
            raise HTTPException(404, f"Unknown action {action}")
        getattr(get_engine(), method)(req.uids)
        return {"ok": True}

    @app.get("/api/tasks/{uid}/error")
    def error_details(uid: str):
        found = get_engine().select([uid])
        if not found:
            raise HTTPException(404, "No such task")
        t = found[0]
        text = f"Task File: {t.filename}\nTask Link: {t.link}\nStatus: {t.status}"
        if t.error_message:
            text += f"\nError Detail: {t.error_message}"
        return {"text": text}

    # ---- settings -----------------------------------------------------
    @app.get("/api/settings")
    def get_settings():
        s = get_engine().settings
        return {k: s.get(k) for k in SETTING_TYPES}

    @app.put("/api/settings")
    def put_settings(updates: dict = Body(...)):
        clean = {}
        for key, value in updates.items():
            kind = SETTING_TYPES.get(key)
            if kind is None:
                continue
            if kind is bool:
                if not isinstance(value, bool):
                    raise HTTPException(400, f"{key} must be true or false")
            elif kind is int:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise HTTPException(400, f"{key} must be a whole number")
                lo, hi = SETTING_RANGES[key]
                if not lo <= value <= hi:
                    raise HTTPException(400, f"{key} must be between {lo} and {hi}")
            elif not isinstance(value, str):
                raise HTTPException(400, f"{key} must be text")
            if key == "theme" and value not in ("Dark", "Light"):
                raise HTTPException(400, "theme must be Dark or Light")
            clean[key] = value
        get_engine().update_settings(clean)
        return get_settings()

    # ---- schedule -----------------------------------------------------
    @app.get("/api/schedule")
    def get_schedule():
        engine = get_engine()
        return {"config": {k: v for k, v in engine.schedule.items() if k != "targets"},
                "targets": engine.schedule.get("targets"),
                "description": offpeak.describe_schedule(engine.schedule)}

    @app.put("/api/schedule")
    def put_schedule(req: ScheduleReq):
        try:
            start_t = offpeak.parse_hhmm(req.start)
            end_t = offpeak.parse_hhmm(req.end)
        except Exception:
            raise HTTPException(400, "Times must be HH:MM")
        if req.recurrence not in ("weekly", "once"):
            raise HTTPException(400, "recurrence must be weekly or once")
        if any(d not in range(7) for d in req.days):
            raise HTTPException(400, "days must be 0 (Mon) to 6 (Sun)")
        if req.enabled:
            if req.recurrence == "weekly" and not req.days:
                raise HTTPException(400, "Pick at least one day, or choose Run once.")
            if req.recurrence == "once":
                try:
                    start_date = dt.date.fromisoformat(req.date)
                except ValueError:
                    raise HTTPException(400, "date must be YYYY-MM-DD")
                end_date = start_date + dt.timedelta(days=1) if end_t <= start_t else start_date
                if dt.datetime.now() >= dt.datetime.combine(end_date, end_t):
                    raise HTTPException(400, "The selected one-off schedule window has already passed.")
        sched = {
            "enabled": req.enabled, "start": req.start, "end": req.end,
            "recurrence": req.recurrence, "days": sorted(set(req.days)),
            "date": req.date, "wake_timer": False, "keep_awake": req.keep_awake,
        }
        get_engine().set_schedule(sched, req.uids or None)
        return get_schedule()

    # ---- CAPTCHA ------------------------------------------------------
    @app.get("/api/captcha")
    def captcha():
        waiting = any(t.status == eng.CAPTCHA_WAIT_STATUS for t in get_engine().tasks)
        return {"waiting": waiting,
                "novnc_port": int(os.environ.get("SILVERSPOON_NOVNC_PORT", "6080"))}

    @app.get("/")
    def index():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))

    app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
