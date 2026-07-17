from PyQt6.QtCore import QThread, pyqtSignal, QTimer, QMetaObject, Qt, Q_ARG
from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, QProgressBar, QMessageBox, QApplication, QTextEdit, QHBoxLayout, QPushButton
import sys
import os
import time
import json
import urllib.request
import tempfile
import zipfile
import subprocess
import shutil
import threading

class UpdateCheckerThread(QThread):
    update_available = pyqtSignal(str, str, str) # version, changelog, download_url
    no_update_found = pyqtSignal()
    error_checking = pyqtSignal(str)
    check_finished = pyqtSignal(float) # Emits the new timestamp to save
    
    def __init__(self, current_version, repo, settings_path, force=False):
        super().__init__()
        self.current_version = current_version
        self.repo = repo
        self.settings_path = settings_path
        self.force = force
        
    def _read_last_check(self):
        try:
            if os.path.exists(self.settings_path):
                with open(self.settings_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return data.get("last_update_check", 0.0)
        except Exception:
            pass
        return 0.0

    def _write_last_check(self, timestamp):
        data = {}
        try:
            if os.path.exists(self.settings_path):
                with open(self.settings_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
        except Exception:
            pass
        data["last_update_check"] = timestamp
        try:
            with open(self.settings_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4)
        except Exception:
            pass
        
    def run(self):
        try:
            if sys.platform != "win32" or (not hasattr(sys, "frozen") and not self.force):
                return
                
            now = time.time()
            last_check = self._read_last_check()
            if not self.force and (now - last_check < 86400):
                return
                
            req = urllib.request.Request(
                f"https://api.github.com/repos/{self.repo}/releases/latest",
                headers={"User-Agent": f"SilverSpoon-Updater/{self.current_version}", "Accept": "application/vnd.github+json"}
            )
            with urllib.request.urlopen(req, timeout=10) as res:
                data = json.loads(res.read().decode())
            
            latest_version = data.get("tag_name", "")
            
            if latest_version and latest_version > self.current_version:
                assets = data.get("assets", [])
                download_url = None
                for asset in assets:
                    name = asset.get("name", "")
                    if "SilverSpoon" in name and name.endswith(".zip"):
                        download_url = asset.get("browser_download_url")
                        break
                        
                if download_url:
                    self.update_available.emit(latest_version, data.get("body", "No changelog provided."), download_url)
                    self._write_last_check(now)
                    self.check_finished.emit(now)
                    return
                    
            if self.force:
                self.no_update_found.emit()
                    
            self._write_last_check(now)
            self.check_finished.emit(now)
            
        except Exception as e:
            self._write_last_check(time.time())
            self.check_finished.emit(time.time())
            if self.force:
                self.error_checking.emit(str(e))

class UpdateDownloaderDialog(QDialog):
    def __init__(self, download_url, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Downloading Update...")
        self.setFixedSize(400, 100)
        self.download_url = download_url
        
        layout = QVBoxLayout(self)
        self.label = QLabel("Downloading latest version, please wait...")
        layout.addWidget(self.label)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        layout.addWidget(self.progress_bar)
        
        self.worker = threading.Thread(target=self.download_update, daemon=True)
        self.worker.start()
        
        self.timer = QTimer()
        self.timer.timeout.connect(self.check_worker)
        self.timer.start(100)
        
        self.error = None
        self.temp_zip = None
        self.finished = False

    def check_worker(self):
        if self.finished:
            self.timer.stop()
            if self.error:
                QMessageBox.critical(self, "Update Error", f"Failed to download update:\n{self.error}")
                self.reject()
            else:
                self.accept()

    def download_update(self):
        try:
            self.temp_zip = os.path.join(tempfile.gettempdir(), f"silverspoon_update_{int(time.time())}.zip")
            req = urllib.request.Request(self.download_url, headers={"User-Agent": "SilverSpoon-Updater"})
            with urllib.request.urlopen(req, timeout=60) as r:
                total_length = r.headers.get("Content-Length")
                
                with open(self.temp_zip, "wb") as f:
                    if total_length is None:
                        f.write(r.read())
                    else:
                        downloaded = 0
                        total_length = int(total_length)
                        while True:
                            chunk = r.read(8192)
                            if not chunk:
                                break
                            downloaded += len(chunk)
                            f.write(chunk)
                            done = int(100 * downloaded / total_length)
                            QMetaObject.invokeMethod(self.progress_bar, "setValue", Qt.ConnectionType.QueuedConnection, Q_ARG(int, done))
                            
        except Exception as e:
            self.error = str(e)
            if self.temp_zip and os.path.exists(self.temp_zip):
                try: os.remove(self.temp_zip)
                except: pass
        finally:
            self.finished = True

