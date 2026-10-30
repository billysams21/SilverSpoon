"""Qt-free download engine shared by the desktop GUI and the web app.

Owns the task queue, the download workers (direct, form-host and
CAPTCHA-solved links), archive extraction and the off-peak scheduler. The
front-ends (pyqt_downloader.MainWindow, webapp.server) only call its public
methods and read ``snapshot()`` / ``events``; nothing here imports PyQt.
"""
import contextlib
import datetime as _dt
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque

from curl_cffi import requests as curl_requests

import scheduler as offpeak
from cf_turnstile import TurnstileSolver
from extraction import list_archive, is_extracted, run_with_progress, ExtractionCancelled
from providers import (
    needs_resolution, needs_form_resolution, resolve_form_link, ResolveError,
    filename_from_link, download_resolved_directly,
    filename_from_content_disposition,
)
from segmented import SegmentedDownload, probe as probe_ranges, state_path as segment_state_path, MIN_SEGMENT

CURRENT_VERSION = "v1.6.0"

# Statuses, grouped the way the UI and the queue logic need them.
DONE_STATUSES = ("Completed", "Extracted", "Extract Cancelled")
FINISHED_STATUSES = ("Completed", "Extracted", "Cancelled", "Extract Cancelled")
ACTIVE_STATUSES = ("Downloading", "Starting...", "Solving CAPTCHA...",
                   "Waiting for you to solve CAPTCHA", "Extracting...")
STARTABLE_STATUSES = ("Queued", "Cancelled", "Error", "Paused", "Pausing...", "CAPTCHA Timeout",
                      "Solving CAPTCHA...", "Waiting for you to solve CAPTCHA")
CAPTCHA_WAIT_STATUS = "Waiting for you to solve CAPTCHA"


# ----------------------------------------------------------------------
# Paths, settings and history
# ----------------------------------------------------------------------
def data_path(name):
    """A state file (settings, history, ...) under SILVERSPOON_DATA_DIR, or the
    home directory when that isn't set."""
    base = os.environ.get("SILVERSPOON_DATA_DIR") or os.path.expanduser("~")
    return os.path.join(base, name)


def get_settings_path():
    return data_path(".silverspoon_settings.json")


def get_history_path():
    return data_path(".silverspoon_history.json")


def get_report_path():
    return data_path(".silverspoon_offpeak_report.jsonl")


def default_download_dir():
    env_dir = os.environ.get("SILVERSPOON_DOWNLOAD_DIR")
    if env_dir:
        return env_dir
    if sys.platform == "win32":
        default_downloads = os.path.join(os.path.expanduser("~"), "Downloads")
        if not os.path.exists(default_downloads):
            try:
                os.makedirs(default_downloads, exist_ok=True)
            except Exception:
                default_downloads = os.path.abspath(".")
        return default_downloads
    return os.path.abspath(".")


def default_settings():
    return {
        "theme": "Dark",
        "default_save_dir": default_download_dir(),
        "max_workers": 3,
        "max_connections": 4,
        "extract_after_download": False,
        "auto_retry_errors": False,
        "captcha_timeout": 10,
        "show_solver_browser": False,
        "column_widths": {},
        "skip_delete_confirmation": False,
        "show_warning_dialog": True,
        "last_update_check": 0.0
    }


def load_settings():
    settings = default_settings()
    settings_path = get_settings_path()
    if os.path.exists(settings_path):
        try:
            with open(settings_path, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                settings.update(loaded)
        except Exception:
            pass

    save_settings(settings)

    return settings


def save_settings(settings):
    settings_path = get_settings_path()
    try:
        os.makedirs(os.path.dirname(settings_path) or ".", exist_ok=True)
        with open(settings_path, 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=4)
    except Exception as e:
        logging.error("Failed to save settings: %s", e)


def format_error_message(error, max_length=160):
    text = str(error).strip()
    lower_text = text.lower()
    error_type = type(error).__name__

    if "connectionabortederror" in lower_text or "10053" in text:
        return "Connection was aborted by your computer or network security software. Try again, or check firewall/antivirus settings."
    if "connection reset" in lower_text or "connectionreseterror" in lower_text:
        return "Connection was reset by the server or your network. Try again later."
    if "timed out" in lower_text or "timeout" in lower_text:
        return "The connection timed out. Check your network and try again."

    if not text:
        return error_type

    text = re.sub(r"\s+", " ", text)
    if len(text) > max_length:
        text = text[:max_length].rstrip() + "..."
    return f"{error_type}: {text}"


class DownloadTask:
    def __init__(self, link, base_save_dir, folder_name=None):
        self.link = link.strip()
        self.base_save_dir = base_save_dir

        self.filename = filename_from_link(self.link)
        self.file_id = self.link.rstrip('/').split('/')[-1].split('#')[0]

        if folder_name:
            self.folder_name = folder_name
        else:
            # Fallback calculate smart directory grouping based on prefix
            match = re.search(r'(.*?)(\.part\d+\.rar|\.rar)$', self.filename, re.IGNORECASE)
            if match:
                self.folder_name = match.group(1).strip('._-')
            else:
                self.folder_name = self.filename.rsplit('.', 1)[0]

        self.save_dir = os.path.normpath(os.path.join(self.base_save_dir, self.folder_name))
        self.filepath = os.path.normpath(os.path.join(self.save_dir, self.filename))

        # Stable per-instance id: survives restart (persisted) and uniquely
        # identifies this task even when two tasks share the same link.
        self.uid = uuid.uuid4().hex
        self.status = "Queued"
        self.progress = 0.0
        self.speed = 0.0
        self.downloaded_bytes = 0
        self.total_bytes = 0
        self.error_message = ""
        self.retry_count = 0

        self.pause_flag = False
        self.cancel_flag = False
        self.tree_item = None   # desktop GUI row, unused by the engine
        self.is_selected = False

        # App-update tasks download a plain URL (no CAPTCHA/direct-link step)
        # and, once complete, offer to install instead of being extracted.
        self.is_update = False
        self.update_version = None
        # Set once the install prompt has been answered (incl. "Not yet") so it
        # isn't shown again on every launch; installing later goes through the
        # task's "Install Update" menu item.
        self.install_handled = False

        # Extraction progress (0-100) while status is "Extracting...", or None
        # when the extractor doesn't report one. Not persisted.
        self.extract_progress = None

    def to_dict(self):
        return {
            "uid": self.uid,
            "link": self.link,
            "base_save_dir": self.base_save_dir,
            "folder_name": self.folder_name,
            "status": self.status,
            "error_message": self.error_message,
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "progress": self.progress,
            "is_update": self.is_update,
            "update_version": self.update_version,
            "install_handled": self.install_handled,
            "filename": self.filename
        }

    @classmethod
    def from_dict(cls, data):
        task = cls(data["link"], data["base_save_dir"], data["folder_name"])
        # Ensure it doesn't auto-start if it was active when closed
        if data["status"] in ("Downloading", "Pending", "Starting...", "Resolving Container...", "Pausing...", "Solving CAPTCHA...", CAPTCHA_WAIT_STATUS):
            task.status = "Paused"
            task.pause_flag = True
        else:
            task.status = data["status"]

        # Keep the persisted uid so scheduled targets survive a restart; older
        # history without one keeps the freshly generated uid.
        if data.get("uid"):
            task.uid = data["uid"]
        task.downloaded_bytes = data.get("downloaded_bytes", 0)
        task.total_bytes = data.get("total_bytes", 0)
        task.progress = data.get("progress", 0.0)
        task.error_message = data.get("error_message", "")
        task.is_update = data.get("is_update", False)
        task.update_version = data.get("update_version")
        task.install_handled = data.get("install_handled", False)
        # A name taken from the server (links like bzzhr.to/<id> carry none).
        if data.get("filename") and data["filename"] != task.filename:
            task.filename = data["filename"]
            task.filepath = os.path.normpath(os.path.join(task.save_dir, task.filename))
        return task


def load_history():
    history_path = get_history_path()
    tasks = []
    if os.path.exists(history_path):
        try:
            with open(history_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                for item_data in data:
                    tasks.append(DownloadTask.from_dict(item_data))
        except Exception:
            pass
    return tasks


def save_history(tasks):
    history_path = get_history_path()
    try:
        os.makedirs(os.path.dirname(history_path) or ".", exist_ok=True)
        with open(history_path, 'w', encoding='utf-8') as f:
            json.dump([t.to_dict() for t in tasks], f, indent=4)
    except Exception as e:
        logging.error("Failed to save history: %s", e)


# ----------------------------------------------------------------------
# Link helpers (pure)
# ----------------------------------------------------------------------
def parse_links(text, html=None):
    """http(s) links from pasted text. ``html`` (rich clipboard data) wins when
    it holds hrefs, as when a table of links is copied from a browser."""
    found = ""
    if html:
        hrefs = re.findall(r'href=[\'"]?([^\'" >]+)', html)
        found = "\n".join(h for h in hrefs if h.startswith('http'))
    if not found:
        found = text or ""
    return [line.strip().lstrip("- ") for line in found.split('\n')
            if line.strip() and line.lstrip("- ").startswith('http')]


def suggest_folder(links):
    """Batch folder name guessed from the first link."""
    if not links:
        return ""
    first_filename = filename_from_link(links[0])
    match = re.search(r'(.*?)(\.part\d+\.rar|\.rar)$', first_filename, re.IGNORECASE)
    if match:
        suggested = match.group(1).strip('._-')
    else:
        suggested = first_filename.rsplit('.', 1)[0]
    return suggested.replace('_--_fitgirl-repacks.site', '').replace('_--_', ' ').replace('_', ' ').strip()


def format_eta(seconds):
    if seconds <= 0 or seconds == float('inf'):
        return "-"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h {m}m"
    elif m > 0:
        return f"{m}m {s}s"
    else:
        return f"{s}s"


def summarize_batch(batch_tasks, all_tasks):
    """Aggregate progress / speed / ETA / status for one batch folder."""
    total_dl = 0
    total_size = 0
    total_speed = 0.0
    all_completed = True
    any_error = False
    any_downloading = False
    extracting_progress = []
    largest_known = max([t.total_bytes for t in batch_tasks] + [0])

    for task in batch_tasks:
        if task.total_bytes > 0:
            total_dl += task.downloaded_bytes
            total_size += task.total_bytes
        elif os.path.splitext(task.filename)[1].lower() in ('.rar', '.zip'):
            # Estimate the size of a part we know nothing about from the largest known one.
            total_size += largest_known
        total_speed += task.speed

        if task.status not in ("Completed", "Extracted", "Extracting...", "Extract Cancelled"):
            all_completed = False
        if task.status == "Extracting...":
            extracting_progress.append(task.extract_progress)
        if "Error" in task.status:
            any_error = True
        if task.status in ("Downloading", "Starting...", "Pending"):
            any_downloading = True

    status = "Queued"
    if all_completed:
        status = "Extracting..." if extracting_progress else "Completed"
    elif any_error:
        status = "Contains Errors"
    elif any_downloading:
        status = "Active"

    progress = (total_dl / total_size * 100) if total_size > 0 else 0.0
    progress_known = True
    if status == "Extracting...":
        # The whole folder extracts together, so its tasks share one value.
        known = [p for p in extracting_progress if p is not None]
        progress = sum(known) / len(known) if known else 0.0
        progress_known = bool(known)

    eta = None
    if any_downloading and total_speed > 0 and total_size > 0:
        remaining = 0
        for t in batch_tasks:
            if t.total_bytes > 0:
                remaining += t.total_bytes - t.downloaded_bytes
            elif os.path.splitext(t.filename)[1].lower() in ('.rar', '.zip'):
                remaining += largest_known
        if remaining > 0:
            eta = remaining / (total_speed * 1024 * 1024)

    return {
        "status": status,
        "progress": progress,
        "progress_known": progress_known,
        "speed": total_speed,
        "eta": eta,
        "downloaded_bytes": total_dl,
        "total_bytes": total_size,
    }


# ----------------------------------------------------------------------
# Engine
# ----------------------------------------------------------------------
class DownloadEngine:
    SCHEDULER_INTERVAL = 30  # seconds between off-peak polls

    def __init__(self, autostart=True):
        self.lock = threading.RLock()
        self.settings = load_settings()
        self.tasks = []
        self.extracted_folders = set()
        # Auto-extract finished batches. Seeded from the saved default; a front-end
        # may flip it at runtime (the desktop checkbox) without changing that default.
        self.auto_extract = bool(self.settings.get("extract_after_download", False))
        self.events = deque(maxlen=50)
        self._event_id = 0
        self._stop = threading.Event()
        self._save_timer = None
        self._save_lock = threading.Lock()

        self.turnstile_solver = TurnstileSolver(
            timeout=self.settings.get("captcha_timeout", 10),
            show_window=self.settings.get("show_solver_browser", False))
        self.dl_session = curl_requests.Session(impersonate="chrome")

        self.schedule = self.settings.get("schedule") or offpeak.default_schedule()
        self.offpeak_controller = offpeak.OffPeakScheduler(self.schedule)
        self.offpeak_session = None
        self.offpeak_session_uids = None
        self._offpeak_probing = False

        self._load_tasks_from_history()
        self._threads = []
        if autostart:
            self.start()

    # -- lifecycle -------------------------------------------------------
    def start(self):
        for target in (self.download_manager, self._scheduler_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()
        with self._save_lock:
            if self._save_timer:
                self._save_timer.cancel()
        self.save_now()
        save_settings(self.settings)
        offpeak.allow_sleep()
        self.turnstile_solver.stop()

    def _load_tasks_from_history(self):
        for task in load_history():
            if task.status == "Extracted":
                self.extracted_folders.add(task.folder_name)
            elif task.status == "Extracting...":
                task.status = "Completed"
            self.tasks.append(task)

    # -- persistence / events -------------------------------------------
    def save_now(self):
        with self.lock:
            tasks = list(self.tasks)
        save_history(tasks)

    def trigger_history_save(self):
        """Debounced history save (500 ms), safe from any thread."""
        with self._save_lock:
            if self._save_timer:
                self._save_timer.cancel()
            self._save_timer = threading.Timer(0.5, self.save_now)
            self._save_timer.daemon = True
            self._save_timer.start()

    def push_event(self, kind, **data):
        with self.lock:
            self._event_id += 1
            self.events.append({"id": self._event_id, "type": kind, **data})

    def events_since(self, last_id):
        with self.lock:
            return [e for e in self.events if e["id"] > last_id]

    # -- queue management ------------------------------------------------
    def select(self, uids):
        """Tasks for the given uids, in queue order."""
        wanted = set(uids or ())
        with self.lock:
            return [t for t in self.tasks if t.uid in wanted]

    def add_links(self, links, save_dir, folder_name):
        save_dir = os.path.abspath(save_dir or self.settings.get("default_save_dir") or ".")
        added = []
        with self.lock:
            for link in links:
                task = DownloadTask(link, save_dir, folder_name)
                self.tasks.append(task)
                added.append(task)
        if added:
            self.trigger_history_save()
        return added

    def start_tasks(self, uids):
        for task in self.select(uids):
            if task.status in STARTABLE_STATUSES:
                task.status = "Pending"
                task.error_message = ""
                task.cancel_flag = False
                task.pause_flag = False

    def pause_tasks(self, uids):
        for task in self.select(uids):
            if task.status in ("Downloading", "Pending", "Starting..."):
                task.pause_flag = True
                task.status = "Pausing..." if task.status == "Downloading" else "Paused"

    def cancel_tasks(self, uids):
        for task in self.select(uids):
            if task.status == "Extracting...":
                # extract_folder notices within ~0.2 s, kills the extractor
                # and sets "Extract Cancelled" (the download itself is kept).
                task.cancel_flag = True
            elif task.status in ("Downloading", "Pending", "Paused", "Starting...", "Queued"):
                task.cancel_flag = True
                task.pause_flag = False
                task.status = "Cancelled"

    def retry_tasks(self, uids):
        for task in self.select(uids):
            if "Error" in task.status or task.status == "CAPTCHA Timeout":
                task.status = "Pending"
                task.error_message = ""
                task.cancel_flag = False
                task.pause_flag = False
                task.retry_count = 0

    _REDOWNLOAD_ACTIVE = {"Downloading", "Pending", "Starting...", "Pausing...", "Extracting..."}

    def redownload_with_progress(self, uids):
        """Tasks a force redownload would throw partial data away from."""
        return [t for t in self.select(uids)
                if t.status not in self._REDOWNLOAD_ACTIVE and t.progress > 0 and t.status != "Error"]

    def force_redownload(self, uids):
        """Delete the partial files and queue the tasks again. Returns counts."""
        redownloaded = skipped = failed = 0
        for task in self.select(uids):
            if task.status in self._REDOWNLOAD_ACTIVE:
                skipped += 1
                continue

            try:
                if os.path.exists(task.filepath):
                    os.remove(task.filepath)
                if os.path.exists(segment_state_path(task.filepath)):
                    os.remove(segment_state_path(task.filepath))
            except Exception as e:
                failed += 1
                task.status = "Error"
                task.error_message = f"Could not delete existing file before redownload. {format_error_message(e)}"
                continue

            task.cancel_flag = False
            task.pause_flag = False
            task.progress = 0.0
            task.speed = 0.0
            task.downloaded_bytes = 0
            task.total_bytes = 0
            task.error_message = ""
            task.status = "Pending"
            # A fresh download of an update asks again when it completes.
            task.install_handled = False
            self.extracted_folders.discard(task.folder_name)
            redownloaded += 1
        return {"queued": redownloaded, "skipped": skipped, "failed": failed}

    def delete_tasks(self, uids, delete_files=False):
        """Cancel and remove the tasks, optionally deleting their files."""
        removed = []
        for task in self.select(uids):
            task.cancel_flag = True
            task.status = "Cancelled"

            if delete_files and os.path.exists(task.filepath):
                try:
                    os.remove(task.filepath)
                    if os.path.exists(segment_state_path(task.filepath)):
                        os.remove(segment_state_path(task.filepath))
                except Exception as e:
                    logging.error("Failed to delete %s: %s", task.filepath, e)

            with self.lock:
                if task in self.tasks:
                    self.tasks.remove(task)
            removed.append(task)
        if removed:
            self.trigger_history_save()
        return removed

    def clear_finished(self):
        with self.lock:
            to_remove = [t for t in self.tasks if t.status in FINISHED_STATUSES]
            for t in to_remove:
                self.tasks.remove(t)
        if to_remove:
            self.trigger_history_save()
        return to_remove

    # -- settings / schedule --------------------------------------------
    def apply_settings(self):
        """Push the current settings into the running solver."""
        self.turnstile_solver.TOKEN_TIMEOUT = self.settings.get("captcha_timeout", 10)
        self.turnstile_solver.show_window = self.settings.get("show_solver_browser", False)

    def update_settings(self, updates):
        """Merge (not replace) so keys the caller doesn't manage, such as the
        off-peak "schedule", are preserved."""
        with self.lock:
            self.settings.update(updates)
            save_settings(self.settings)
            self.apply_settings()
            if "extract_after_download" in updates:
                self.auto_extract = bool(updates["extract_after_download"])

    def set_schedule(self, schedule, targets=None):
        """None targets = the whole queue; a list of uids = only those tasks."""
        with self.lock:
            self.schedule = dict(schedule)
            self.schedule["targets"] = targets
            self.settings["schedule"] = self.schedule
            save_settings(self.settings)
            self.offpeak_controller.update(self.schedule)
        self.push_event("schedule_changed")

    # -- snapshot for the front-ends ------------------------------------
    def snapshot(self):
        with self.lock:
            tasks = list(self.tasks)
            events = list(self.events)
        batches = {}
        for t in tasks:
            batches.setdefault(t.folder_name, []).append(t)

        task_rows = []
        global_speed = 0.0
        for t in tasks:
            extracting = t.status == "Extracting..."
            if t.status == "Downloading":
                global_speed += t.speed
            eta = None
            if t.status == "Downloading" and t.speed > 0 and t.total_bytes > 0:
                eta = (t.total_bytes - t.downloaded_bytes) / (t.speed * 1024 * 1024)
            task_rows.append({
                "uid": t.uid,
                "folder": t.folder_name,
                "filename": t.filename,
                "link": t.link,
                "status": t.status,
                "error": t.error_message,
                "progress": (t.extract_progress or 0.0) if extracting else t.progress,
                "progress_text": ((f"{t.extract_progress:.1f}%" if t.extract_progress is not None else "-")
                                  if extracting
                                  else (f"{t.progress:.1f}%" if t.status not in ("Extracted", "Extract Error") else "-")),
                "speed": t.speed if t.status == "Downloading" else 0.0,
                "eta": eta,
                "downloaded_bytes": t.downloaded_bytes,
                "total_bytes": t.total_bytes,
            })

        batch_rows = []
        for name, batch_tasks in batches.items():
            row = summarize_batch(batch_tasks, tasks)
            row["name"] = name
            batch_rows.append(row)

        return {
            "tasks": task_rows,
            "batches": batch_rows,
            "stats": {
                "active": sum(1 for t in tasks if t.status in ACTIVE_STATUSES),
                "queued": sum(1 for t in tasks if t.status in ("Queued", "Pending")),
                "done": sum(1 for t in tasks if t.status in DONE_STATUSES),
                "speed": global_speed,
                "captcha_waiting": any(t.status == CAPTCHA_WAIT_STATUS for t in tasks),
            },
            "schedule": {
                "config": {k: v for k, v in self.schedule.items() if k != "targets"},
                "targets": self.schedule.get("targets"),
                "description": offpeak.describe_schedule(self.schedule),
                "window_active": bool(self.offpeak_controller._active),
            },
            "events": events,
            "version": CURRENT_VERSION,
        }

    # -- download manager -----------------------------------------------
    def download_manager(self):
        while not self._stop.is_set():
            # CAPTCHA resolution belongs to the same worker slot as the actual
            # transfer. Otherwise each resolving task stops counting as active
            # and the manager can exceed the configured concurrency limit.
            max_workers = self.settings.get("max_workers", 3)
            with self.lock:
                tasks = list(self.tasks)
            active = sum(
                1 for t in tasks
                if t.status in ("Downloading", "Starting...", "Solving CAPTCHA...", CAPTCHA_WAIT_STATUS)
            )
            if active < max_workers:
                for task in tasks:
                    if task.status == "Pending":
                        task.status = "Starting..."
                        threading.Thread(target=self.download_worker, args=(task,), daemon=True).start()
                        active += 1
                        if active >= max_workers:
                            break

            # Check for extraction
            if self.auto_extract:
                self.check_extraction()

            self._stop.wait(1)

    # -- extraction ------------------------------------------------------
    def extract_preview(self, uids):
        """Folders among the selection that are already extracted (so the
        caller can confirm before overwriting them)."""
        return [name for name, folder_tasks in self._extract_targets(uids).items()
                if self._already_extracted(folder_tasks)]

    def _extract_targets(self, uids):
        folders = {}
        for task in self.select(uids):
            folders.setdefault(task.folder_name, []).append(task)
        to_extract = {}
        for folder_name, tasks_in_folder in folders.items():
            if any(t.status == "Extracting..." for t in tasks_in_folder):
                continue
            with self.lock:
                to_extract[folder_name] = [t for t in self.tasks if t.folder_name == folder_name]
        return to_extract

    def extract(self, uids, skip_folders=()):
        """Extract the selected batches now, even over existing files."""
        started = []
        for folder_name, all_folder_tasks in self._extract_targets(uids).items():
            if folder_name in skip_folders:
                continue
            self.extracted_folders.add(folder_name)
            threading.Thread(target=self.extract_folder, args=(all_folder_tasks, True), daemon=True).start()
            started.append(folder_name)
        return started

    def check_extraction(self):
        # Group tasks by folder
        folders = {}
        with self.lock:
            tasks = list(self.tasks)
        for task in tasks:
            folders.setdefault(task.folder_name, []).append(task)

        for folder_name, tasks_in_folder in folders.items():
            if folder_name in self.extracted_folders:
                continue

            # Never auto-extract an app-update download.
            if any(getattr(t, "is_update", False) for t in tasks_in_folder):
                continue

            valid_extraction_statuses = {"Completed", "Extracted", "Extracting..."}
            if tasks_in_folder and all(t.status in valid_extraction_statuses for t in tasks_in_folder):
                if all(t.status == "Extracted" for t in tasks_in_folder):
                    self.extracted_folders.add(folder_name)
                    continue

                # If ANY task in this folder is currently Extracting..., don't spawn another thread
                if any(t.status == "Extracting..." for t in tasks_in_folder):
                    continue

                self.extracted_folders.add(folder_name)
                threading.Thread(target=self.extract_folder, args=(tasks_in_folder,), daemon=True).start()

    @staticmethod
    def _archive_volumes(save_dir):
        """First volume of each archive in save_dir, i.e. what the extractor is given."""
        files = sorted(os.listdir(save_dir))
        vols = []
        for f in files:
            # 1. Main multipart start (.part01.rar, .part1.rar)
            if re.search(r'\.part0*1\.rar$', f, re.IGNORECASE):
                vols.append(os.path.join(save_dir, f))
            # 2. Sequential start (.001)
            elif re.search(r'\.001$', f):
                vols.append(os.path.join(save_dir, f))
            # 3. Standalone .rar or .zip (not part of a sequence)
            elif f.lower().endswith(('.rar', '.zip')) and not re.search(r'\.part\d+\.rar$', f, re.IGNORECASE):
                vols.append(os.path.join(save_dir, f))
        if not vols and files:
            # Fallback to just the first file alphabetically
            vols.append(os.path.join(save_dir, files[0]))
        return vols

    @staticmethod
    def _find_extractor():
        """(extractor_type, command, seven_zip) for this platform; extractor_type
        is None if nothing is installed. seven_zip reads archive listings, also
        when WinRAR does the extracting, and is None if no 7-Zip is available."""
        extractor_type = None
        base_cmd = None
        bundled_7z = None
        if sys.platform == 'win32':
            # Windows: prefer installed 7-Zip > WinRAR > bundled 7z.exe
            if hasattr(sys, '_MEIPASS'):
                bundled_7z = os.path.join(sys._MEIPASS, '7z.exe')
            else:
                bundled_7z = os.path.join(os.path.dirname(os.path.abspath(__file__)), '7z.exe')
            installed_7z = r"C:\Program Files\7-Zip\7z.exe"
            installed_winrar = r"C:\Program Files\WinRAR\WinRAR.exe"
            if os.path.exists(installed_7z):
                extractor_type = '7z'
                base_cmd = installed_7z
            elif os.path.exists(installed_winrar):
                extractor_type = 'winrar'
                base_cmd = installed_winrar
            elif os.path.exists(bundled_7z):
                extractor_type = '7z'
                base_cmd = bundled_7z
        else:
            # 7z (p7zip, or Ubuntu's 7zip package); Debian 12's 7zip only has 7zz.
            seven_zip_cmd = next((n for n in ('7z', '7zz', '7za') if shutil.which(n)), None)
            if seven_zip_cmd:
                extractor_type = '7z'
                base_cmd = seven_zip_cmd
            elif shutil.which('unrar'):
                extractor_type = 'unrar'
                base_cmd = 'unrar'

        if extractor_type == '7z':
            seven_zip = base_cmd
        elif bundled_7z and os.path.exists(bundled_7z):
            seven_zip = bundled_7z
        else:
            seven_zip = next((shutil.which(n) for n in ('7z', '7zz', '7za') if shutil.which(n)), None)
        return extractor_type, base_cmd, seven_zip

    def _already_extracted(self, tasks_in_folder):
        """True if the folder is marked Extracted, or every file in its
        archives is already on disk with the right size."""
        if all(t.status == "Extracted" for t in tasks_in_folder):
            return True
        save_dir = tasks_in_folder[0].save_dir
        try:
            vols = self._archive_volumes(save_dir)
        except OSError:
            return False
        seven_zip = self._find_extractor()[2]
        if not vols or not seven_zip:
            return False
        for vol in vols:
            entries = list_archive(seven_zip, vol)
            if not (entries and is_extracted(entries, save_dir)):
                return False
        return True

    def extract_folder(self, tasks_in_folder, force=False):
        save_dir = tasks_in_folder[0].save_dir
        folder_name = tasks_in_folder[0].folder_name

        for t in tasks_in_folder:
            t.status = "Extracting..."
            t.extract_progress = None
            t.cancel_flag = False

        # Cancel (or Delete) on any task of the folder stops its extraction.
        def should_stop():
            return any(t.cancel_flag for t in tasks_in_folder)

        try:
            vols_to_extract = self._archive_volumes(save_dir)
            if not vols_to_extract:
                for t in tasks_in_folder:
                    t.status = "Extract Error (No File)"
                    t.error_message = f"No archive file was found in {save_dir}."
                self.extracted_folders.discard(folder_name)
                return

            extractor_type, base_cmd, seven_zip = self._find_extractor()
            if not extractor_type:
                for t in tasks_in_folder:
                    t.status = "Extract Error (No extractor found)"
                    t.error_message = "No supported extractor was found. Install 7-Zip or WinRAR, then retry extraction."
                self.extracted_folders.discard(folder_name)
                return

            listings = [list_archive(seven_zip, vol) if seven_zip else None for vol in vols_to_extract]

            # Auto-extract skips archives whose files are already all on disk
            # with matching sizes (e.g. extracted earlier, outside the app or
            # before the history was reset).
            if not force and all(entries and is_extracted(entries, save_dir) for entries in listings):
                for t in tasks_in_folder:
                    t.status = "Extracted"
                    t.error_message = ""
                    t.extract_progress = None
                self.trigger_history_save()
                return

            # Weight each archive by its unpacked size so the bar moves evenly
            # across a folder with several archives.
            weights = [sum(size for _, size in entries) if entries else 0 for entries in listings]
            if not all(weights):
                weights = [1] * len(vols_to_extract)
            total_weight = sum(weights)
            done_weight = 0

            # Extract each base volume found
            for vol, weight in zip(vols_to_extract, weights):
                if should_stop():
                    raise ExtractionCancelled()
                if extractor_type == '7z':
                    cmd = [base_cmd, 'x', vol, f'-o{save_dir}', '-y', '-bsp1', '-bso0']
                elif extractor_type == 'winrar':
                    cmd = [base_cmd, 'x', '-y', vol, f'{save_dir}\\']
                elif extractor_type == 'unrar':
                    cmd = [base_cmd, 'x', vol, f'{save_dir}/', '-y']

                def report(percent, base=done_weight, weight=weight):
                    value = (base + weight * percent / 100) / total_weight * 100
                    for t in tasks_in_folder:
                        t.extract_progress = value

                returncode, output = run_with_progress(cmd, report, should_stop)
                if returncode != 0:
                    raise subprocess.CalledProcessError(returncode, cmd, output=output)
                done_weight += weight

            for t in tasks_in_folder:
                t.status = "Extracted"
                t.error_message = ""
                t.extract_progress = None
            self.trigger_history_save()

        except ExtractionCancelled:
            # Partly extracted files stay on disk; "Extract Now" redoes it. The
            # folder stays in extracted_folders and the status isn't one
            # check_extraction picks up, so auto-extract won't restart it.
            for t in tasks_in_folder:
                if t.status == "Extracting...":  # a deleted task is already "Cancelled"
                    t.status = "Extract Cancelled"
                t.extract_progress = None
                t.cancel_flag = False
            self.trigger_history_save()
        except subprocess.CalledProcessError as e:
            logging.error(f"Extraction error (subprocess): {e}\n{(e.output or '')[-500:]}", exc_info=True)
            for t in tasks_in_folder:
                t.status = "Extract Error (Corrupt?)"
                t.error_message = f"Extractor failed with exit code {e.returncode}. The archive may be corrupt, incomplete, or password-protected."
            self.extracted_folders.discard(folder_name)
            self.trigger_history_save()
        except Exception as e:
            logging.error(f"Extraction error: {e}", exc_info=True)
            for t in tasks_in_folder:
                t.status = "Extract Error"
                t.error_message = f"Extraction failed: {format_error_message(e)}"
            self.extracted_folders.discard(folder_name)
            self.trigger_history_save()

    # -- downloading -----------------------------------------------------
    def get_direct_link(self, task):
        try:
            task.status = "Solving CAPTCHA..."

            def notify(waiting):
                # The solver shows its window when a check needs a human click.
                task.status = CAPTCHA_WAIT_STATUS if waiting else "Solving CAPTCHA..."

            result = self.turnstile_solver.get_direct_link(task.link, notify=notify)
            direct_link = result.get("direct_url")
            if direct_link:
                # Stash cookies/UA on the task for the download transport
                task._dl_cookies = result.get("cookies", {})
                task._dl_user_agent = result.get("user_agent", "")
                return direct_link
            task.status = "CAPTCHA Timeout"
            task.error_message = "The file host did not return a direct download link. The link may be expired or unavailable."
        except Exception as e:
            logging.error(f"Error getting direct link for {task.link}: {e}", exc_info=True)
            if "after " in str(e) and " seconds" in str(e):
                task.status = "CAPTCHA Timeout"
            else:
                task.status = "Error"
            task.error_message = f"Could not get the direct download link. {format_error_message(e)}"
            return None
        if not task.error_message:
            task.status = "Error"
            task.error_message = "Could not get the direct download link. The link may be expired or blocked."
        return None

    def _download_direct_file(self, task, url=None):
        """Download a plain, direct URL via urllib (stdlib), with resume,
        pause/cancel and progress. Used for app updates, form hosts (url is
        then the resolved link) and any non-resolver host. Kept off the shared
        curl_cffi session, which hangs (curl 28) for a manager-spawned worker
        that skips the get_direct_link/nodriver path."""
        try:
            os.makedirs(task.save_dir, exist_ok=True)
        except Exception as e:
            task.status = "Error"
            task.error_message = (
                f"Failed to create download folder '{task.save_dir}'. "
                f"{format_error_message(e)}")
            self.trigger_history_save()
            return

        url = url or task.link
        headers = {"User-Agent": f"SilverSpoon/{CURRENT_VERSION}"}
        self._adopt_server_filename(task, url, headers)
        if self._download_segmented(task, url, headers):
            return

        initial_size = os.path.getsize(task.filepath) if os.path.exists(task.filepath) else 0
        req = urllib.request.Request(url, headers=headers)
        if initial_size > 0:
            req.add_header("Range", f"bytes={initial_size}-")

        task.status = "Downloading"
        task.error_message = ""
        try:
            with contextlib.closing(urllib.request.urlopen(req, timeout=30)) as r:
                status = getattr(r, "status", 200) or 200
                content_range = r.headers.get("Content-Range")
                content_length = r.headers.get("Content-Length")
                if status == 206 and content_range:
                    m = re.search(r'/([0-9]+)$', content_range)
                    task.total_bytes = int(m.group(1)) if m else 0
                    mode = "ab"
                else:
                    # Server ignored Range (or none asked) -> full body, restart.
                    task.total_bytes = int(content_length) if content_length else 0
                    initial_size = 0
                    mode = "wb"
                task.downloaded_bytes = initial_size

                now = time.time()
                samples = deque([(now, task.downloaded_bytes)])
                with open(task.filepath, mode) as f:
                    while True:
                        if task.pause_flag:
                            task.status = "Paused"; task.speed = 0; return
                        if task.cancel_flag:
                            task.status = "Cancelled"; task.speed = 0; return
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        task.downloaded_bytes += len(chunk)
                        now = time.time()
                        samples.append((now, task.downloaded_bytes))
                        while len(samples) > 1 and now - samples[0][0] > 3:
                            samples.popleft()
                        w0t, w0b = samples[0]
                        dur = now - w0t
                        if dur > 0:
                            task.speed = ((task.downloaded_bytes - w0b) / dur) / (1024 * 1024)
                        if task.total_bytes > 0:
                            task.progress = (task.downloaded_bytes / task.total_bytes) * 100

            task.progress = 100
            task.speed = 0
            task.status = "Completed"
            task.error_message = ""
            self.trigger_history_save()
        except urllib.error.HTTPError as he:
            if he.code == 416 and initial_size > 0:
                # Range not satisfiable -> the file is already fully downloaded.
                task.total_bytes = initial_size
                task.downloaded_bytes = initial_size
                task.progress = 100
                task.speed = 0
                task.status = "Completed"
                task.error_message = ""
                self.trigger_history_save()
                return
            self._update_download_failed(task, he)
        except Exception as e:
            self._update_download_failed(task, e)

    def _adopt_server_filename(self, task, url, headers):
        """Links like bzzhr.to/<id> carry no file name, so the task would be
        named after the id with no extension. Before anything is written, take
        the name the server sends (Content-Disposition) instead."""
        if os.path.splitext(task.filename)[1] or os.path.exists(task.filepath) \
                or os.path.exists(segment_state_path(task.filepath)):
            return
        try:
            req = urllib.request.Request(url, headers={**headers, "Range": "bytes=0-0"})
            with contextlib.closing(urllib.request.urlopen(req, timeout=30)) as r:
                name = filename_from_content_disposition(r.headers.get("Content-Disposition"))
        except Exception as e:
            logging.warning("Could not read the server file name for %s: %s", task.link, e)
            return
        if name and name != task.filename:
            task.filename = name
            task.filepath = os.path.normpath(os.path.join(task.save_dir, name))
            self.trigger_history_save()

    def _download_segmented(self, task, url, headers):
        """Download over several connections when the server supports ranges.
        Returns True when it handled the task (finished, paused, cancelled or
        failed) and False to fall back to the single-connection path."""
        connections = max(1, int(self.settings.get("max_connections", 4)))
        state = segment_state_path(task.filepath)
        resuming = os.path.exists(state)
        if connections <= 1 and not resuming:
            return False
        try:
            total, ranges = probe_ranges(url, headers)
        except Exception as e:
            if not resuming:
                return False  # let the single-connection path report it
            self._update_download_failed(task, e)
            return True
        if not ranges or total <= 0:
            if resuming:
                # A half-done segmented file can't be continued without ranges;
                # its size isn't the downloaded byte count, so start over.
                for p in (state, task.filepath):
                    if os.path.exists(p):
                        os.remove(p)
            return False
        if total < 2 * MIN_SEGMENT and not resuming:
            return False  # too small to be worth splitting

        task.status = "Downloading"
        task.error_message = ""
        task.total_bytes = total
        samples = deque()

        def on_progress(done):
            now = time.time()
            task.downloaded_bytes = done
            task.progress = done / total * 100
            samples.append((now, done))
            while len(samples) > 1 and now - samples[0][0] > 3:
                samples.popleft()
            dur = now - samples[0][0]
            if dur > 0:
                task.speed = ((done - samples[0][1]) / dur) / (1024 * 1024)

        # Same per-download share of the global limit as the curl transport.
        limit_mb_s = self.settings.get("bandwidth_limit", 0)
        max_workers = self.settings.get("max_workers", 3)
        bytes_per_second = (limit_mb_s * 1024 * 1024) / max_workers if limit_mb_s > 0 and max_workers > 0 else 0

        try:
            finished = SegmentedDownload(
                url, task.filepath, total, connections=connections, headers=headers,
                should_stop=lambda: task.pause_flag or task.cancel_flag,
                on_progress=on_progress, bytes_per_second=bytes_per_second,
            ).run()
        except Exception as e:
            task.speed = 0
            self._update_download_failed(task, e)
            return True
        task.speed = 0
        if not finished:
            # Progress is saved beside the file; Start/Resume continues it.
            task.status = "Cancelled" if task.cancel_flag else "Paused"
            return True
        task.downloaded_bytes = total
        task.progress = 100
        task.status = "Completed"
        task.error_message = ""
        self.trigger_history_save()
        return True

    def _update_download_failed(self, task, exc):
        logging.error("Direct download error for %s: %s", task.link, exc, exc_info=True)
        if task.cancel_flag or task.pause_flag:
            return
        if self.settings.get("auto_retry_errors", False) and task.retry_count < 3:
            task.retry_count += 1
            task.status = "Pending"
            task.error_message = ""
        else:
            task.status = "Error"
            what = "Update download" if getattr(task, "is_update", False) else "Download"
            task.error_message = f"{what} failed. {format_error_message(exc)}"
        self.trigger_history_save()

    def _resolve_form_link(self, task):
        """Direct URL for a form host (FileKeeper) link, or None if it failed
        or the task was paused/cancelled during the countdown. Resolved afresh
        on every start/resume, so an expired signed link never sticks."""
        def wait(seconds):
            end = time.time() + seconds
            while time.time() < end and not (task.cancel_flag or task.pause_flag):
                time.sleep(0.25)

        try:
            url = resolve_form_link(task.link, sleep=wait)
        except Exception as e:
            logging.error("Form host resolution error for %s: %s", task.link, e, exc_info=True)
            if task.cancel_flag or task.pause_flag:
                return None
            if self.settings.get("auto_retry_errors", False) and task.retry_count < 3:
                task.retry_count += 1
                task.status = "Pending"
                task.error_message = ""
            else:
                task.status = "Error"
                detail = str(e) if isinstance(e, ResolveError) else format_error_message(e)
                task.error_message = f"Could not get the direct download link. {detail}"
            self.trigger_history_save()
            return None
        if task.cancel_flag or task.pause_flag:
            return None  # cancel/pause already set the status
        return url

    def download_worker(self, task):
        # App updates and any non-resolver (general direct) URL download straight
        # over HTTP via urllib; form hosts (FileKeeper) first trade the page for
        # a direct link over plain HTTP. Only FuckingFast/DataNodes-style links
        # go through the Turnstile/CAPTCHA solver + curl transport.
        if getattr(task, "is_update", False) or not needs_resolution(task.link):
            url = None
            if not getattr(task, "is_update", False) and needs_form_resolution(task.link):
                url = self._resolve_form_link(task)
                if not url:
                    return
            self._download_direct_file(task, url)
            return

        dl_url = self.get_direct_link(task)
        if not dl_url:
            if not task.cancel_flag and not task.pause_flag:
                # Don't auto-retry a CAPTCHA timeout: re-solving immediately just
                # respawns the browser and rarely succeeds.
                if (self.settings.get("auto_retry_errors", False)
                        and task.retry_count < 3
                        and task.status != "CAPTCHA Timeout"):
                    task.retry_count += 1
                    task.status = "Pending"
                    task.error_message = ""
                    logging.info(f"Auto-retrying {task.filename} (attempt {task.retry_count}/3) after missing direct link")
                else:
                    if task.status != "CAPTCHA Timeout":
                        task.status = "Error"
                    if not task.error_message:
                        task.error_message = "Could not get the direct download link."
            return

        if task.cancel_flag:
            task.status = "Cancelled"
            return

        if task.pause_flag:
            task.status = "Paused"
            return

        if download_resolved_directly(task.link):
            # bzzhr.to: the resolved link needs no browser cookies, so use the
            # direct downloader (resume, multiple connections, server file name).
            self._download_direct_file(task, dl_url)
            return

        task.status = "Downloading"
        task.error_message = ""

        try:
            if not os.path.exists(task.save_dir):
                try:
                    os.makedirs(task.save_dir, exist_ok=True)
                except Exception as e:
                    if self.settings.get("auto_retry_errors", False) and task.retry_count < 3:
                        task.retry_count += 1
                        task.status = "Pending"
                        task.error_message = ""
                        logging.info(f"Auto-retrying {task.filename} (attempt {task.retry_count}/3) after directory error")
                    else:
                        task.status = "Error"
                        task.error_message = f"Failed to create save directory '{task.save_dir}'. {format_error_message(e)}"
                    self.trigger_history_save()
                    return

            initial_size = 0
            if os.path.exists(task.filepath):
                initial_size = os.path.getsize(task.filepath)

            headers = {}
            if getattr(task, '_dl_user_agent', None):
                headers['User-Agent'] = task._dl_user_agent

            head_req = self.dl_session.head(dl_url, cookies=getattr(task, '_dl_cookies', {}), headers=headers, allow_redirects=True)
            # Some hosts reject HEAD while accepting the actual ranged GET. Only
            # trust Content-Length when the HEAD request itself succeeded.
            total_size = 0
            if 200 <= head_req.status_code < 300:
                try:
                    total_size = int(head_req.headers.get('content-length', 0))
                except (TypeError, ValueError):
                    total_size = 0
            task.total_bytes = total_size

            if initial_size > 0 and initial_size == total_size:
                task.downloaded_bytes = total_size
                task.progress = 100
                task.status = "Completed"
                task.error_message = ""
                return

            resume_header = headers.copy()
            mode = 'wb'
            if initial_size > 0:
                resume_header['Range'] = f'bytes={initial_size}-'
                mode = 'ab'

            with contextlib.closing(self.dl_session.get(dl_url, stream=True, headers=resume_header, cookies=getattr(task, '_dl_cookies', {}))) as r:
                if r.status_code == 416 and initial_size > 0:
                    content_range = r.headers.get('content-range', '')
                    match = re.search(r'/([0-9]+)$', content_range)
                    if match and initial_size == int(match.group(1)):
                        task.total_bytes = initial_size
                        task.downloaded_bytes = initial_size
                        task.progress = 100
                        task.speed = 0
                        task.status = "Completed"
                        task.error_message = ""
                        self.trigger_history_save()
                        return
                if r.status_code not in (200, 206):
                    if r.status_code in (403, 503):
                        preview = r.text[:500] if hasattr(r, 'text') else "No text body"
                        logging.error(f"Download 403/503 for {dl_url}. Body preview: {preview}")

                    if self.settings.get("auto_retry_errors", False) and task.retry_count < 3:
                        task.retry_count += 1
                        task.status = "Pending"
                        task.error_message = ""
                        logging.info(f"Auto-retrying {task.filename} (attempt {task.retry_count}/3) after HTTP {r.status_code}")
                    else:
                        task.status = "Error"
                        task.error_message = f"Download request failed. Server returned HTTP {r.status_code}."
                    return

                if r.status_code == 200 and initial_size > 0:
                    # server ignores range header, restart from beginning
                    mode = 'wb'
                    initial_size = 0

                task.downloaded_bytes = initial_size
                if total_size == 0:
                    content_range = r.headers.get('content-range', '')
                    match = re.search(r'/([0-9]+)$', content_range)
                    if match:
                        task.total_bytes = int(match.group(1))
                    else:
                        try:
                            task.total_bytes = int(r.headers.get('content-length', 0)) + initial_size
                        except (TypeError, ValueError):
                            task.total_bytes = 0

                start_time = time.time()
                last_time = start_time
                bytes_since_last = 0
                # Keep a short history instead of reporting only the latest
                # half-second burst of socket data.
                speed_samples = deque([(start_time, task.downloaded_bytes)])

                with open(task.filepath, mode) as f:
                    limit_mb_s = self.settings.get("bandwidth_limit", 0)
                    max_workers = self.settings.get("max_workers", 3)

                    limit_bytes_s = 0
                    if limit_mb_s > 0 and max_workers > 0:
                        limit_bytes_s = (limit_mb_s * 1024 * 1024) / max_workers

                    for chunk in r.iter_content(chunk_size=8192*8):
                        if task.pause_flag:
                            task.status = "Paused"
                            task.speed = 0
                            return
                        if task.cancel_flag:
                            task.status = "Cancelled"
                            task.speed = 0
                            return

                        if chunk:
                            f.write(chunk)
                            size = len(chunk)
                            task.downloaded_bytes += size
                            bytes_since_last += size

                            now = time.time()

                            # Simple Token Bucket / Sleep for Bandwidth Limiting
                            if limit_bytes_s > 0:
                                expected_time = bytes_since_last / limit_bytes_s
                                actual_time = now - last_time
                                if expected_time > actual_time:
                                    time.sleep(expected_time - actual_time)
                                    now = time.time()

                            speed_samples.append((now, task.downloaded_bytes))
                            while len(speed_samples) > 1 and now - speed_samples[0][0] > 3:
                                speed_samples.popleft()
                            window_start, window_bytes = speed_samples[0]
                            window_duration = now - window_start
                            if window_duration > 0:
                                task.speed = (
                                    (task.downloaded_bytes - window_bytes) / window_duration
                                ) / (1024 * 1024)
                            if task.total_bytes > 0:
                                task.progress = (task.downloaded_bytes / task.total_bytes) * 100

                            if now - last_time > 0.5:
                                last_time = now
                                bytes_since_last = 0

                task.progress = 100
                task.speed = 0
                task.status = "Completed"
                task.error_message = ""
                self.trigger_history_save()

        except Exception as e:
            logging.error(f"Download worker error for task {task.link}: {e}", exc_info=True)
            if not task.cancel_flag and not task.pause_flag:
                if self.settings.get("auto_retry_errors", False) and task.retry_count < 3:
                    task.retry_count += 1
                    task.status = "Pending"
                    task.error_message = ""
                    logging.info(f"Auto-retrying {task.filename} (attempt {task.retry_count}/3)")
                else:
                    task.status = "Error"
                    task.error_message = f"Download failed. {format_error_message(e)}"
                self.trigger_history_save()

    # -- off-peak scheduler ---------------------------------------------
    def _scheduler_loop(self):
        # A first poll shortly after start so an app opened mid-window (or
        # launched by a wake timer) starts downloading without waiting a full
        # interval.
        if self._stop.wait(1.5):
            return
        while True:
            try:
                self.scheduler_tick()
            except Exception:
                logging.exception("Scheduler tick failed")
            if self._stop.wait(self.SCHEDULER_INTERVAL):
                return

    def _scheduled_tasks(self):
        """Tasks the active schedule targets: all, or only the chosen uids."""
        targets = self.schedule.get("targets")
        with self.lock:
            if not targets:
                return list(self.tasks)
            targets = set(targets)
            return [t for t in self.tasks if t.uid in targets]

    def _task_key(self, task):
        # Stable uid, not link: two tasks can share a link (re-added/duplicate),
        # which would collide in the session's byte/completion accounting.
        return task.uid

    def scheduler_tick(self):
        now = _dt.datetime.now()
        if self.offpeak_session is not None:
            with self.lock:
                global_speed = sum(t.speed for t in self.tasks if t.status == "Downloading")
            self.offpeak_session.sample_speed(global_speed)
        edge = self.offpeak_controller.poll(now)
        if edge == "open":
            self._offpeak_open(now)
        elif edge == "close":
            self._offpeak_close(now)
        # Lets a front-end refresh its schedule indicator.
        if edge is not None:
            self.push_event("schedule_edge", edge=edge)

    def _offpeak_open(self, now):
        # Probe connectivity off the polling thread (~1.5 s) so a slow probe
        # never delays the next tick.
        if self._offpeak_probing:
            return
        self._offpeak_probing = True

        def probe():
            try:
                if offpeak.check_connection():
                    self._do_offpeak_open(now)
                else:
                    # No connection yet: the next tick (still inside the window)
                    # will re-fire "open" and retry.
                    self.offpeak_controller.cancel_open()
                    logging.warning("Off-peak: no internet connection at window open; will retry.")
            finally:
                self._offpeak_probing = False

        threading.Thread(target=probe, daemon=True).start()

    def _do_offpeak_open(self, now):
        if self.schedule.get("keep_awake", True):
            offpeak.prevent_sleep()

        scheduled = self._scheduled_tasks()
        session = offpeak.OffPeakSession(now)
        for task in scheduled:
            key = self._task_key(task)
            session.snapshot[key] = task.downloaded_bytes
            if task.status in DONE_STATUSES:
                session.completed_before.add(key)
        self.offpeak_session = session
        self.offpeak_session_uids = {t.uid for t in scheduled}

        started = 0
        for task in scheduled:
            if task.status in ("Queued", "Paused", "Cancelled", "Error",
                               "CAPTCHA Timeout"):
                task.status = "Pending"
                task.error_message = ""
                task.cancel_flag = False
                task.pause_flag = False
                started += 1
        logging.info("Download window opened; queued %d task(s).", started)
        self.push_event("schedule_edge", edge="open")

    def _offpeak_close(self, now):
        offpeak.allow_sleep()
        # Pause only the tasks this window was responsible for.
        uids = self.offpeak_session_uids
        with self.lock:
            tasks = list(self.tasks)
        for task in tasks:
            if uids is not None and task.uid not in uids:
                continue
            if task.status in ("Downloading", "Pending", "Starting..."):
                task.pause_flag = True
                task.status = "Pausing..." if task.status == "Downloading" else "Paused"

        session = self.offpeak_session
        self.offpeak_session = None
        if session is None:
            return

        scheduled = [t for t in tasks if uids is None or t.uid in uids]
        bytes_now = {self._task_key(t): t.downloaded_bytes for t in scheduled}
        completed_now = {self._task_key(t) for t in scheduled if t.status in DONE_STATUSES}
        summary = offpeak.summarize(session, now, bytes_now, completed_now)
        report_path = get_report_path()
        report_saved = offpeak.append_report(summary, report_path)
        if not report_saved:
            logging.warning("Failed to append off-peak report to %s", report_path)
        logging.info("Scheduled download window closed; %s", summary)
        self.push_event("offpeak_summary", summary=summary,
                        report_path=report_path if report_saved else None)
