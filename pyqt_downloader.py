import sys
import os
import time
import threading
import re
import subprocess
import json
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTextEdit, QTableWidget,
    QTableWidgetItem, QHeaderView, QFileDialog, QAbstractItemView,
    QCheckBox, QDialog, QFormLayout, QSpinBox, QDialogButtonBox,
    QMessageBox, QInputDialog
)
from PyQt6.QtGui import QAction, QDesktopServices
from PyQt6.QtCore import Qt, QTimer, QUrl

import cloudscraper
from playwright.sync_api import sync_playwright

def get_settings_path():
    return os.path.expanduser("~/.fitgirl_downloader_settings.json")

def load_settings():
    default_settings = {
        "default_save_dir": os.path.abspath("."),
        "max_workers": 3,
        "extract_after_download": False
    }
    settings_path = get_settings_path()
    if os.path.exists(settings_path):
        try:
            with open(settings_path, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                # Merge defaults with loaded to handle missing keys in older configs
                default_settings.update(loaded)
        except Exception:
            pass
    return default_settings

def save_settings(settings):
    settings_path = get_settings_path()
    try:
        with open(settings_path, 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=4)
    except Exception as e:
        print(f"Failed to save settings: {e}")

class SettingsDialog(QDialog):
    def __init__(self, current_settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(400)
        
        self.current_settings = current_settings
        
        layout = QFormLayout(self)
        
        # Save Directory
        dir_layout = QHBoxLayout()
        self.dir_input = QLineEdit(self.current_settings.get("default_save_dir", "."))
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self.browse_dir)
        dir_layout.addWidget(self.dir_input)
        dir_layout.addWidget(browse_btn)
        layout.addRow("Default Save Directory:", dir_layout)
        
        # Max Workers
        self.workers_spinbox = QSpinBox()
        self.workers_spinbox.setRange(1, 10)
        self.workers_spinbox.setValue(self.current_settings.get("max_workers", 3))
        layout.addRow("Max Concurrent Downloads:", self.workers_spinbox)
        
        # Extract Option
        self.extract_checkbox = QCheckBox()
        self.extract_checkbox.setChecked(self.current_settings.get("extract_after_download", False))
        layout.addRow("Extract after download by default:", self.extract_checkbox)
        
        # Buttons
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addRow(button_box)

    def browse_dir(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Save Directory", self.dir_input.text())
        if folder:
            self.dir_input.setText(os.path.abspath(folder))
            
    def get_updated_settings(self):
        return {
            "default_save_dir": self.dir_input.text(),
            "max_workers": self.workers_spinbox.value(),
            "extract_after_download": self.extract_checkbox.isChecked()
        }

class DownloadTask:
    def __init__(self, link, base_save_dir, folder_name=None):
        self.link = link.strip()
        self.base_save_dir = base_save_dir
        
        self.file_id = self.link.split('/')[-1].split('#')[0]
        
        # Check if it's a filecrypt link
        self.is_filecrypt = "filecrypt.cc" in self.link.lower()
        if self.is_filecrypt:
            self.filename = f"Filecrypt Container ({self.file_id})"
            self.folder_name = folder_name if folder_name else "Filecrypt_Containers"
            self.status = "Queued (Container)"
        else:
            self.filename = self.link.split('#')[-1] if '#' in self.link else self.file_id
            if folder_name:
                self.folder_name = folder_name
            else:
                # Fallback calculate smart directory grouping based on prefix
                match = re.search(r'(.*?)(\.part\d+\.rar|\.rar)$', self.filename, re.IGNORECASE)
                if match:
                    self.folder_name = match.group(1).strip('._-')
                else:
                    self.folder_name = self.filename.rsplit('.', 1)[0]
            self.status = "Queued"  # Queued, Pending, Starting..., Downloading, Paused, Cancelled, Completed, Extracting..., Extracted, Error
            
        self.save_dir = os.path.normpath(os.path.join(self.base_save_dir, self.folder_name))
        self.filepath = os.path.normpath(os.path.join(self.save_dir, self.filename))
        
        self.progress = 0.0
        self.speed = 0.0
        self.downloaded_bytes = 0
        self.total_bytes = 0
        
        self.pause_flag = False
        self.cancel_flag = False
        self.row_idx = None
        self.is_selected = False

class MainWindow(QMainWindow):
    # Signals to safely update UI from background threads
    filecrypt_links_found = pyqtSignal(list, str)
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FuckingFast Downloader - UI (PyQt6)")
        self.resize(1000, 650)
        
        self.settings = load_settings()
        
        self.tasks = []
        self.max_workers = self.settings.get("max_workers", 3)
        self.scraper = cloudscraper.create_scraper(browser='chrome')
        self.is_all_selected = False
        self.extracted_folders = set()
        
        self.filecrypt_links_found.connect(self.add_extracted_filecrypt_links)
        
        self.setup_ui()
        
        # Start Background Download Manager
        self.manager_thread = threading.Thread(target=self.download_manager, daemon=True)
        self.manager_thread.start()
        
        # UI Updater Timer
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_ui)
        self.timer.start(500) # update every 500ms

    def setup_ui(self):
        # Menu Bar Setup
        menu_bar = self.menuBar()
        
        # File Menu
        file_menu = menu_bar.addMenu("&File")
        
        import_action = QAction("&Import Links from File...", self)
        import_action.triggered.connect(self.import_links_from_file)
        file_menu.addAction(import_action)
        
        settings_action = QAction("&Settings", self)
        settings_action.triggered.connect(self.open_settings_dialog)
        file_menu.addAction(settings_action)
        
        file_menu.addSeparator()
        
        exit_action = QAction("&Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
        
        # Help Menu
        help_menu = menu_bar.addMenu("&Help")
        
        github_action = QAction("&GitHub Repository", self)
        github_action.triggered.connect(self.open_github_link)
        help_menu.addAction(github_action)
        
        contact_action = QAction("&Contact Us", self)
        contact_action.triggered.connect(self.open_contact_link)
        help_menu.addAction(contact_action)
        
        contributing_action = QAction("C&ontributing Guide", self)
        contributing_action.triggered.connect(self.show_contributing_dialog)
        help_menu.addAction(contributing_action)
        
        help_menu.addSeparator()
        
        about_action = QAction("&About", self)
        about_action.triggered.connect(self.show_about_dialog)
        help_menu.addAction(about_action)
        
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        
        # 1. Directory Section
        dir_layout = QHBoxLayout()
        dir_layout.addWidget(QLabel("Base Save Directory:"))
        self.dir_input = QLineEdit(self.settings.get("default_save_dir", os.path.abspath(".")))
        dir_layout.addWidget(self.dir_input)
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self.browse_dir)
        dir_layout.addWidget(browse_btn)
        main_layout.addLayout(dir_layout)
        
        # 2. Links Section
        main_layout.addWidget(QLabel("Paste Links Here (one per line):"))
        self.text_links = QTextEdit()
        self.text_links.setMaximumHeight(80)
        main_layout.addWidget(self.text_links)
        
        add_btn = QPushButton("Add Links to Queue")
        add_btn.setStyleSheet("background-color: #2e55cc; color: white; font-weight: bold; padding: 6px;")
        add_btn.clicked.connect(self.add_links)
        main_layout.addWidget(add_btn)
        
        # 3. Table Section
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["Sel", "Filename", "Status", "Progress", "Speed", "Size"])
        
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 30)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.cellClicked.connect(self.handle_cell_clicked)
        main_layout.addWidget(self.table)
        
        # 4. Action Section
        action_layout = QHBoxLayout()
        
        self.select_all_btn = QPushButton("Select All")
        self.select_all_btn.clicked.connect(self.toggle_select_all)
        action_layout.addWidget(self.select_all_btn)
        
        self.start_btn = QPushButton("Start / Resume")
        self.start_btn.setStyleSheet("background-color: #2ecc71; color: white; font-weight: bold; padding: 6px;")
        self.start_btn.clicked.connect(self.start_downloads)
        action_layout.addWidget(self.start_btn)
        
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setStyleSheet("background-color: #f39c12; color: white; font-weight: bold; padding: 6px;")
        self.pause_btn.clicked.connect(self.pause_selected)
        action_layout.addWidget(self.pause_btn)
        
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setStyleSheet("background-color: #e74c3c; color: white; font-weight: bold; padding: 6px;")
        self.cancel_btn.clicked.connect(self.cancel_selected)
        action_layout.addWidget(self.cancel_btn)
        
        action_layout.addStretch()
        
        self.extract_checkbox = QCheckBox("Extract after download")
        self.extract_checkbox.setChecked(self.settings.get("extract_after_download", False))
        action_layout.addWidget(self.extract_checkbox)
        
        clear_btn = QPushButton("Clear Completed")
        clear_btn.clicked.connect(self.clear_finished)
        action_layout.addWidget(clear_btn)
        
        main_layout.addLayout(action_layout)

    def add_extracted_filecrypt_links(self, links, save_dir):
        for link in links:
            if not any(t.link == link for t in self.tasks):
                task = DownloadTask(link, save_dir)
                task.status = "Pending"  # Auto-start them
                
                row = self.table.rowCount()
                self.table.insertRow(row)
                task.row_idx = row
                
                chk_item = QTableWidgetItem()
                chk_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
                chk_item.setCheckState(Qt.CheckState.Checked)
                task.is_selected = True
                
                self.table.setItem(row, 0, chk_item)
                self.table.setItem(row, 1, QTableWidgetItem(f"{task.folder_name} / {task.filename}"))
                self.table.setItem(row, 2, QTableWidgetItem(task.status))
                self.table.setItem(row, 3, QTableWidgetItem("0%"))
                self.table.setItem(row, 4, QTableWidgetItem("-"))
                self.table.setItem(row, 5, QTableWidgetItem("-"))
                
                self.tasks.append(task)

    def import_links_from_file(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Import Links", "", "Text Files (*.txt);;All Files (*)")
        if file_path:
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                    # Append to existing text or set it if empty
                    current_text = self.text_links.toPlainText()
                    if current_text.strip():
                        self.text_links.setText(current_text + "\n" + content)
                    else:
                        self.text_links.setText(content)
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to read file:\n{e}")

    def open_github_link(self):
        QDesktopServices.openUrl(QUrl("https://github.com/billysams21/FitGirlDownloader"))
        
    def open_contact_link(self):
        QDesktopServices.openUrl(QUrl("https://github.com/billysams21/FitGirlDownloader/issues"))

    def show_contributing_dialog(self):
        QMessageBox.information(self, "Contributing Guide",
            "<h3>Contributing to FitGirlDownloader</h3>"
            "<p>We welcome contributions! Here is how you can help:</p>"
            "<ul>"
            "<li><b>Bug Reports:</b> Use the 'Contact Us' button to open an issue on GitHub. Please include steps to reproduce the error.</li>"
            "<li><b>Feature Requests:</b> Have an idea? Open an issue on GitHub and tag it as an enhancement!</li>"
            "<li><b>Pull Requests:</b>"
            "  <ol>"
            "    <li>Fork the repository.</li>"
            "    <li>Create a new branch for your feature or bug fix.</li>"
            "    <li>Test your changes locally.</li>"
            "    <li>Submit a Pull Request with a clear description of your changes.</li>"
            "  </ol>"
            "</li>"
            "</ul>"
            "<p><i>Note: Please ensure your code follows the existing style and does not break current functionality.</i></p>"
        )

    def show_about_dialog(self):
        QMessageBox.about(self, "About FuckingFast Downloader",
            "<h3>FuckingFast Downloader v1.1</h3>"
            "<p>A simple, fast downloader for FuckingFast links.</p>"
            "<p>Select your links, paste them in, and hit Add!</p>"
            "<hr>"
            "<h4>Changelog (v1.1):</h4>"
            "<ul>"
            "<li><b>New:</b> Settings page with persistent configurations (Save Directory, Max Concurrent Downloads, Auto-extract).</li>"
            "<li><b>New:</b> Import links directly from .txt files via the File menu.</li>"
            "<li><b>New:</b> Batch Folder Prompt! Automatically groups main game parts and optional files into the exact same folder when adding links.</li>"
            "<li><b>Changed:</b> Consolidated 'Start' and 'Resume' into a single, smarter action button.</li>"
            "<li><b>Changed:</b> Improved top menu bar layout.</li>"
            "</ul>"
        )

    def open_settings_dialog(self):
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec():
            # User clicked Save
            self.settings = dialog.get_updated_settings()
            save_settings(self.settings)
            
            # Apply immediate UI/State updates
            self.max_workers = self.settings.get("max_workers", 3)
            self.dir_input.setText(self.settings.get("default_save_dir", "."))
            self.extract_checkbox.setChecked(self.settings.get("extract_after_download", False))

    def browse_dir(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Save Directory", self.dir_input.text())
        if folder:
            self.dir_input.setText(os.path.abspath(folder))

    def add_links(self):
        text = self.text_links.toPlainText().strip()
        if not text:
            return
            
        links = [line.strip() for line in text.split('\n') if line.strip() and line.startswith('http')]
        if not links:
            return
            
        save_dir = os.path.abspath(self.dir_input.text())
        
        # Try to guess a folder name from the first link
        suggested_folder = ""
        first_link = links[0]
        first_filename = first_link.split('#')[-1] if '#' in first_link else first_link.split('/')[-1].split('#')[0]
        match = re.search(r'(.*?)(\.part\d+\.rar|\.rar)$', first_filename, re.IGNORECASE)
        if match:
            suggested_folder = match.group(1).strip('._-')
        else:
            suggested_folder = first_filename.rsplit('.', 1)[0]
            
        # Prompt user for batch folder name
        folder_name, ok = QInputDialog.getText(
            self, 
            "Batch Folder Name", 
            "Enter a folder name for these files:\n(This groups main game and optional files together)",
            QLineEdit.EchoMode.Normal,
            suggested_folder
        )
        
        if not ok or not folder_name.strip():
            return # User cancelled
            
        folder_name = folder_name.strip()
        
        for link in links:
            task = DownloadTask(link, save_dir, folder_name)
            row = self.table.rowCount()
            self.table.insertRow(row)
            task.row_idx = row
            
            chk_item = QTableWidgetItem()
            chk_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk_item.setCheckState(Qt.CheckState.Unchecked)
            
            self.table.setItem(row, 0, chk_item)
            self.table.setItem(row, 1, QTableWidgetItem(f"{task.folder_name} / {task.filename}"))
            self.table.setItem(row, 2, QTableWidgetItem(task.status))
            self.table.setItem(row, 3, QTableWidgetItem("0%"))
            self.table.setItem(row, 4, QTableWidgetItem("-"))
            self.table.setItem(row, 5, QTableWidgetItem("-"))
            
            self.tasks.append(task)
            
        self.text_links.clear()

    def toggle_select_all(self):
        self.is_all_selected = not self.is_all_selected
        state = Qt.CheckState.Checked if self.is_all_selected else Qt.CheckState.Unchecked
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item:
                item.setCheckState(state)

    def handle_cell_clicked(self, row, col):
        if col == 0:
            item = self.table.item(row, col)
            task = next((t for t in self.tasks if t.row_idx == row), None)
            if task:
                task.is_selected = (item.checkState() == Qt.CheckState.Checked)

    def get_selected_tasks(self):
        selected = []
        for task in self.tasks:
            if task.row_idx is not None:
                item = self.table.item(task.row_idx, 0)
                if item and item.checkState() == Qt.CheckState.Checked:
                    selected.append(task)
        return selected

    def start_downloads(self):
        for task in self.get_selected_tasks():
            if task.status in ("Queued", "Queued (Container)", "Cancelled", "Error", "Paused"):
                task.status = "Pending"
                task.cancel_flag = False
                task.pause_flag = False

    def pause_selected(self):
        for task in self.get_selected_tasks():
            if task.status in ("Downloading", "Pending", "Starting...", "Resolving Container..."):
                task.pause_flag = True
                task.status = "Pausing..." if task.status == "Downloading" else "Paused"

    def cancel_selected(self):
        for task in self.get_selected_tasks():
            if task.status in ("Downloading", "Pending", "Paused", "Starting...", "Queued", "Queued (Container)", "Resolving Container..."):
                task.cancel_flag = True
                task.pause_flag = False
                task.status = "Cancelled"

    def clear_finished(self):
        to_remove = [t for t in self.tasks if t.status in ("Completed", "Extracted", "Cancelled")]
        to_remove.sort(key=lambda t: t.row_idx, reverse=True)
        for t in to_remove:
            self.table.removeRow(t.row_idx)
            self.tasks.remove(t)
            
        for idx, t in enumerate(self.tasks):
            t.row_idx = idx

    def update_ui(self):
        for task in self.tasks:
            if task.row_idx is None:
                continue
            prog_str = f"{task.progress:.1f}%" if task.status not in ("Extracted", "Extracting...", "Extract Error") else "-"
            speed_str = f"{task.speed:.2f} MB/s" if task.status == "Downloading" else "-"
            size_mb = task.total_bytes / (1024*1024)
            dl_mb = task.downloaded_bytes / (1024*1024)
            size_str = f"{dl_mb:.1f} / {size_mb:.1f} MB" if task.total_bytes > 0 else "-"
            
            self.table.item(task.row_idx, 2).setText(task.status)
            self.table.item(task.row_idx, 3).setText(prog_str)
            self.table.item(task.row_idx, 4).setText(speed_str)
            self.table.item(task.row_idx, 5).setText(size_str)

    def download_manager(self):
        while True:
            active = sum(1 for t in self.tasks if t.status in ("Downloading", "Starting...", "Resolving Container..."))
            if active < self.max_workers:
                for task in self.tasks:
                    if task.status == "Pending":
                        if getattr(task, 'is_filecrypt', False):
                            task.status = "Resolving Container..."
                            threading.Thread(target=self.resolve_filecrypt_worker, args=(task,), daemon=True).start()
                        else:
                            task.status = "Starting..."
                            threading.Thread(target=self.download_worker, args=(task,), daemon=True).start()
                        active += 1
                        if active >= self.max_workers:
                            break
            
            # Check for extraction
            if self.extract_checkbox.isChecked():
                self.check_extraction()
                
            time.sleep(1)
            
    def check_extraction(self):
        # Group tasks by folder
        folders = {}
        for task in self.tasks:
            if task.folder_name not in folders:
                folders[task.folder_name] = []
            folders[task.folder_name].append(task)
            
        for folder_name, tasks_in_folder in folders.items():
            if folder_name in self.extracted_folders:
                continue
                
            # If all tasks in this group are downloaded/completed
            if all(t.status in ("Completed", "Extracted") for t in tasks_in_folder):
                self.extracted_folders.add(folder_name)
                threading.Thread(target=self.extract_folder, args=(tasks_in_folder,), daemon=True).start()

    def extract_folder(self, tasks_in_folder):
        save_dir = tasks_in_folder[0].save_dir
        
        for t in tasks_in_folder:
            t.status = "Extracting..."
            
        try:
            files = os.listdir(save_dir)
            files.sort()
            
            first_vol = None
            for f in files:
                if re.search(r'\.part0*1\.rar$', f, re.IGNORECASE) or \
                   re.search(r'\.001$', f) or \
                   (f.lower().endswith('.rar') and not re.search(r'\.part\d+\.rar$', f, re.IGNORECASE)):
                    first_vol = os.path.join(save_dir, f)
                    break
                    
            if not first_vol and files:
                # Fallback to just the first file alphabetically
                first_vol = os.path.join(save_dir, files[0])
                
            if not first_vol:
                for t in tasks_in_folder:
                    t.status = "Extract Error (No File)"
                return
                
            # Define paths to extractors
            # Check for bundled 7z.exe (PyInstaller extracts it to sys._MEIPASS in temp dir)
            if hasattr(sys, '_MEIPASS'):
                bundled_7z = os.path.join(sys._MEIPASS, '7z.exe')
            else:
                bundled_7z = os.path.join(os.path.dirname(os.path.abspath(__file__)), '7z.exe')
                
            installed_7z = r"C:\Program Files\7-Zip\7z.exe"
            installed_winrar = r"C:\Program Files\WinRAR\WinRAR.exe"
            
            cmd = None
            # Prioritize full installed 7-Zip because 7za (standalone) often fails on newer multi-volume RARs
            if os.path.exists(installed_7z):
                cmd = [installed_7z, 'x', first_vol, f'-o{save_dir}', '-y']
            elif os.path.exists(installed_winrar):
                cmd = [installed_winrar, 'x', '-y', first_vol, f'{save_dir}\\']
            elif os.path.exists(bundled_7z):
                cmd = [bundled_7z, 'x', first_vol, f'-o{save_dir}', '-y']
                
            if not cmd:
                for t in tasks_in_folder:
                    t.status = "Extract Error (Missing 7z.exe)"
                return
                
            # Run extraction silently without spawning a console window
            creationflags = 0x08000000 # subprocess.CREATE_NO_WINDOW
            subprocess.run(
                cmd, 
                check=True, 
                creationflags=creationflags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL
            )
            
            for t in tasks_in_folder:
                t.status = "Extracted"
                
        except subprocess.CalledProcessError:
            for t in tasks_in_folder:
                t.status = "Extract Error (Corrupt?)"
        except Exception as e:
            for t in tasks_in_folder:
                t.status = f"Extract Error"

    def resolve_filecrypt_worker(self, task):
        try:
            real_links = []
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=False)
                context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
                page = context.new_page()
                
                page.goto(task.link)
                
                # Check for Captcha
                pow_box = page.locator(".pow-captcha__box")
                if pow_box.count() > 0:
                    try:
                        # Attempt JS click first to avoid invisible ad overlays
                        pow_box.evaluate("element => element.click()")
                    except:
                        # Fallback to physical click, but catch the ad popup!
                        try:
                            with page.expect_popup(timeout=3000) as popup_info:
                                pow_box.click(force=True)
                            popup_info.value.close()
                            # Click it again now that the ad is gone
                            pow_box.evaluate("element => element.click()")
                        except:
                            pass
                            
                    # Close any random popups that spawn while waiting
                    def handle_popup(popup):
                        try:
                            popup.close()
                        except:
                            pass
                    page.on("popup", handle_popup)
                            
                    # Wait continuously as long as the captcha box is visible on the screen
                    while True:
                        if task.cancel_flag:
                            browser.close()
                            task.status = "Cancelled"
                            return
                        time.sleep(1)
                        if page.locator(".pow-captcha__box").count() == 0:
                            print("Captcha box disappeared!")
                            break
                            
                    # Remove the popup handler before moving to the links phase
                    page.remove_listener("popup", handle_popup)
                    
                    # Give it a couple extra seconds to fully load the table after the captcha resolves
                    time.sleep(3)
                else:
                    time.sleep(3) # Wait for page to render if no captcha
                
                # We expect a table of files now
                # Let's target the buttons specifically for "fuckingfast" or generic download buttons
                # Based on the user's recording, the button is inside a row containing the filename and host.
                
                # Find all buttons that contain /Link/ or onclick=openLink
                links = page.locator("a[href^='/Link/'], a[onclick*='openLink'], button[onclick*='openLink']").all()
                
                # If there are none, maybe it's just one big redirect link
                if not links:
                    print("No explicit link buttons found. Checking table rows...")
                    rows = page.locator("tr").all()
                    for row in rows:
                        if "fuckingfast" in row.inner_text().lower():
                            btn = row.locator("button, a").first
                            if btn.count() > 0:
                                links.append(btn)
                
                print(f"Found {len(links)} links to process.")
                
                for i, l in enumerate(links):
                    if task.cancel_flag:
                        break
                    try:
                        # Click the link, expect a popup
                        with page.expect_popup(timeout=15000) as popup_info:
                            try:
                                l.evaluate("element => element.click()")
                            except:
                                l.click(force=True)
                        
                        popup = popup_info.value
                        popup.wait_for_load_state("domcontentloaded")
                        time.sleep(2)
                        
                        # Handle the interstitial "Go to website" or "Skip ad" screen
                        for _ in range(3):
                            if "fuckingfast.co" in popup.url:
                                break
                            
                            # Check for "Go to website"
                            skip_link = popup.get_by_role("link", name="Go to website")
                            if skip_link.count() > 0:
                                try:
                                    with popup.expect_popup(timeout=5000) as ad_info:
                                        skip_link.click(force=True)
                                    ad_info.value.close() # Close the ad popup
                                except Exception:
                                    pass
                                time.sleep(1)
                                
                            # Check for "Skip ad"
                            skip_btn = popup.get_by_text("Skip ad")
                            if skip_btn.count() > 0:
                                try:
                                    skip_btn.click(force=True)
                                except:
                                    pass
                                time.sleep(2)
                        
                        final_url = popup.url
                        if "fuckingfast.co" in final_url:
                            # Strip cloudflare tracking tokens if they get appended
                            clean_url = final_url.split('?')[0]
                            if '#' in final_url:
                                clean_url += '#' + final_url.split('#')[-1]
                            real_links.append(clean_url)
                        else:
                            # Not a fuckingfast link or still an ad, close it
                            pass
                        popup.close()
                    except Exception as e:
                        try:
                            popup.close()
                        except:
                            pass
                browser.close()
                
            if real_links:
                task.status = "Completed"
                self.filecrypt_links_found.emit(real_links, task.base_save_dir)
            else:
                if not task.cancel_flag:
                    task.status = "Error (No Links)"
                    
        except Exception as e:
            if not task.cancel_flag:
                task.status = f"Error: {str(e)[:20]}"

    def get_direct_link(self, task):
        try:
            res = self.scraper.get(task.link)
            if res.status_code != 200:
                return None
            
            post_url = f"https://fuckingfast.co/f/{task.file_id}/go"
            headers = {
                'HX-Request': 'true',
                'HX-Target': '',
                'HX-Current-URL': task.link,
                'Referer': task.link
            }
            res2 = self.scraper.post(post_url, headers=headers)
            if res2.status_code == 200:
                return res2.headers.get('Hx-Redirect')
        except Exception:
            return None
        return None

    def download_worker(self, task):
        dl_url = self.get_direct_link(task)
        if not dl_url:
            if not task.cancel_flag and not task.pause_flag:
                task.status = "Error"
            return
            
        if task.cancel_flag:
            task.status = "Cancelled"
            return
            
        if task.pause_flag:
            task.status = "Paused"
            return

        task.status = "Downloading"
        
        try:
            if not os.path.exists(task.save_dir):
                os.makedirs(task.save_dir, exist_ok=True)
                
            initial_size = 0
            if os.path.exists(task.filepath):
                initial_size = os.path.getsize(task.filepath)
                
            head_req = self.scraper.head(dl_url)
            total_size = int(head_req.headers.get('content-length', 0))
            task.total_bytes = total_size
            
            if initial_size > 0 and initial_size == total_size:
                task.downloaded_bytes = total_size
                task.progress = 100
                task.status = "Completed"
                return
                
            resume_header = {}
            mode = 'wb'
            if initial_size > 0:
                resume_header = {'Range': f'bytes={initial_size}-'}
                mode = 'ab'
                
            with self.scraper.get(dl_url, stream=True, headers=resume_header) as r:
                if r.status_code not in (200, 206):
                    task.status = "Error"
                    return
                    
                if r.status_code == 200 and initial_size > 0:
                    mode = 'wb'
                    initial_size = 0
                    
                task.downloaded_bytes = initial_size
                if total_size == 0 and 'content-length' in r.headers:
                    task.total_bytes = int(r.headers['content-length']) + initial_size
                elif total_size == 0:
                    task.total_bytes = 0
                    
                start_time = time.time()
                last_time = start_time
                bytes_since_last = 0
                
                with open(task.filepath, mode) as f:
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
                            if now - last_time > 0.5:
                                task.speed = (bytes_since_last / (now - last_time)) / (1024*1024)
                                if task.total_bytes > 0:
                                    task.progress = (task.downloaded_bytes / task.total_bytes) * 100
                                last_time = now
                                bytes_since_last = 0
                
                task.progress = 100
                task.speed = 0
                task.status = "Completed"
                
        except Exception as e:
            if not task.cancel_flag and not task.pause_flag:
                task.status = "Error"

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
