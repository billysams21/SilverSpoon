import sys
import os
import time
import threading
import re
import subprocess
import json
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTextEdit, QTreeWidget,
    QTreeWidgetItem, QHeaderView, QFileDialog, QAbstractItemView,
    QCheckBox, QDialog, QFormLayout, QSpinBox, QDialogButtonBox,
    QMessageBox, QInputDialog
)
from PyQt6.QtGui import QAction, QDesktopServices
from PyQt6.QtCore import Qt, QTimer, QUrl

import cloudscraper

def get_settings_path():
    return os.path.expanduser("~/.fitgirl_downloader_settings.json")

def load_settings():
    default_settings = {
        "default_save_dir": os.path.abspath("."),
        "max_workers": 3,
        "extract_after_download": False,
        "column_widths": {},
        "skip_delete_confirmation": False
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
        
        # Skip Delete Confirmation Option
        self.skip_delete_checkbox = QCheckBox()
        self.skip_delete_checkbox.setChecked(self.current_settings.get("skip_delete_confirmation", False))
        layout.addRow("Skip delete confirmation:", self.skip_delete_checkbox)
        
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
            "extract_after_download": self.extract_checkbox.isChecked(),
            "skip_delete_confirmation": self.skip_delete_checkbox.isChecked(),
            "column_widths": self.current_settings.get("column_widths", {})
        }

class DownloadTask:
    def __init__(self, link, base_save_dir, folder_name=None):
        self.link = link.strip()
        self.base_save_dir = base_save_dir
        
        self.file_id = self.link.split('/')[-1].split('#')[0]
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
            
        self.save_dir = os.path.normpath(os.path.join(self.base_save_dir, self.folder_name))
        self.filepath = os.path.normpath(os.path.join(self.save_dir, self.filename))
        
        self.status = "Queued"
        self.progress = 0.0
        self.speed = 0.0
        self.downloaded_bytes = 0
        self.total_bytes = 0
        
        self.pause_flag = False
        self.cancel_flag = False
        self.tree_item = None
        self.is_selected = False

    def to_dict(self):
        return {
            "link": self.link,
            "base_save_dir": self.base_save_dir,
            "folder_name": self.folder_name,
            "status": self.status,
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "progress": self.progress
        }
        
    @classmethod
    def from_dict(cls, data):
        task = cls(data["link"], data["base_save_dir"], data["folder_name"])
        # Ensure it doesn't auto-start if it was active when closed
        if data["status"] in ("Downloading", "Pending", "Starting...", "Resolving Container..."):
            task.status = "Paused"
            task.pause_flag = True
        else:
            task.status = data["status"]
            
        task.downloaded_bytes = data.get("downloaded_bytes", 0)
        task.total_bytes = data.get("total_bytes", 0)
        task.progress = data.get("progress", 0.0)
        return task

def get_history_path():
    return os.path.expanduser("~/.fitgirl_downloader_history.json")

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
        with open(history_path, 'w', encoding='utf-8') as f:
            json.dump([t.to_dict() for t in tasks], f, indent=4)
    except Exception as e:
        print(f"Failed to save history: {e}")

class MainWindow(QMainWindow):
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
        
        self.setup_ui()
        self.load_tasks_from_history()
        
        # Start Background Download Manager
        self.manager_thread = threading.Thread(target=self.download_manager, daemon=True)
        self.manager_thread.start()
        
        # UI Updater Timer
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_ui)
        self.timer.start(500) # update every 500ms

    def closeEvent(self, event):
        # Save tasks history before closing
        save_history(self.tasks)
        
        # Save column widths
        col_widths = {}
        for i in range(self.tree.columnCount()):
            col_widths[str(i)] = self.tree.columnWidth(i)
        self.settings["column_widths"] = col_widths
        save_settings(self.settings)
        
        event.accept()

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
        
        # 2. Links & Global Stats Section
        stats_layout = QHBoxLayout()
        stats_layout.addWidget(QLabel("Paste Links Here (one per line):"))
        
        paste_btn = QPushButton("Paste from Clipboard")
        paste_btn.clicked.connect(self.paste_from_clipboard)
        stats_layout.addWidget(paste_btn)
        
        stats_layout.addStretch()
        self.global_speed_label = QLabel("Global Speed: 0.00 MB/s")
        self.global_speed_label.setStyleSheet("font-weight: bold; color: #2ecc71;")
        stats_layout.addWidget(self.global_speed_label)
        main_layout.addLayout(stats_layout)
        
        self.text_links = QTextEdit()
        self.text_links.setAcceptRichText(False) # Prevents styling from being retained
        self.text_links.setMaximumHeight(80)
        main_layout.addWidget(self.text_links)
        
        add_btn = QPushButton("Add Links to Queue")
        add_btn.setStyleSheet("background-color: #2e55cc; color: white; font-weight: bold; padding: 6px;")
        add_btn.clicked.connect(self.add_links)
        main_layout.addWidget(add_btn)
        
        # 3. Table/Tree Section
        self.tree = QTreeWidget()
        self.tree.setColumnCount(7)
        self.tree.setHeaderLabels(["Filename / Folder", "Sel", "Status", "Progress", "Speed", "ETA", "Size"])
        
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.tree.setColumnWidth(1, 40)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.tree.header().setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        self.tree.header().setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        self.tree.header().setSectionResizeMode(5, QHeaderView.ResizeMode.Interactive)
        self.tree.header().setSectionResizeMode(6, QHeaderView.ResizeMode.Interactive)
        
        # Load saved column widths if available
        saved_widths = self.settings.get("column_widths", {})
        if saved_widths:
            for i in range(self.tree.columnCount()):
                width = saved_widths.get(str(i))
                if width:
                    self.tree.setColumnWidth(i, width)
        else:
            # Default widths
            self.tree.setColumnWidth(0, 300)
            self.tree.setColumnWidth(2, 100)
            self.tree.setColumnWidth(3, 80)
            self.tree.setColumnWidth(4, 80)
            self.tree.setColumnWidth(5, 80)
            self.tree.setColumnWidth(6, 120)
        
        # Move the 'Sel' (checkbox) column visually to the far left
        self.tree.header().moveSection(1, 0)
        
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        
        # Remove dotted focus box on clicked cells
        self.tree.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        
        self.tree.itemClicked.connect(self.handle_item_clicked)
        # Apply a stylesheet to ensure checkboxes are centered in their new logical column
        # Also remove any outline when an item is selected
        self.tree.setStyleSheet("""
            QTreeView::indicator { width: 16px; height: 16px; }
            QTreeView::item:selected { outline: none; }
        """)
        main_layout.addWidget(self.tree)
        
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
        
        self.retry_btn = QPushButton("Retry Error")
        self.retry_btn.setStyleSheet("background-color: #9b59b6; color: white; font-weight: bold; padding: 6px;")
        self.retry_btn.clicked.connect(self.retry_selected)
        action_layout.addWidget(self.retry_btn)
        
        self.delete_btn = QPushButton("🗑️ Delete")
        self.delete_btn.setStyleSheet("background-color: #34495e; color: white; font-weight: bold; padding: 6px;")
        self.delete_btn.clicked.connect(self.delete_selected)
        action_layout.addWidget(self.delete_btn)
        
        action_layout.addStretch()
        
        self.extract_checkbox = QCheckBox("Extract after download")
        self.extract_checkbox.setChecked(self.settings.get("extract_after_download", False))
        action_layout.addWidget(self.extract_checkbox)
        
        clear_btn = QPushButton("Clear Completed")
        clear_btn.clicked.connect(self.clear_finished)
        action_layout.addWidget(clear_btn)
        
        main_layout.addLayout(action_layout)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_selected()
        else:
            super().keyPressEvent(event)

    def get_or_create_batch_item(self, folder_name):
        # Search for existing top-level item with this folder name
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item.text(0) == folder_name:
                return item
                
        # Create a new top-level item for this batch
        batch_item = QTreeWidgetItem(self.tree)
        batch_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        batch_item.setText(0, folder_name)
        batch_item.setCheckState(1, Qt.CheckState.Unchecked)
        batch_item.setExpanded(True)
        return batch_item

    def add_task_to_ui(self, task):
        batch_item = self.get_or_create_batch_item(task.folder_name)
        
        child_item = QTreeWidgetItem(batch_item)
        child_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        
        child_item.setText(0, task.filename)
        
        # Determine initial check state
        check_state = Qt.CheckState.Checked if task.is_selected else Qt.CheckState.Unchecked
        child_item.setCheckState(1, check_state)
        
        child_item.setText(2, task.status)
        child_item.setText(3, "0%")
        child_item.setText(4, "-")
        child_item.setText(5, "-")
        child_item.setText(6, "-")
        
        task.tree_item = child_item
        if task not in self.tasks:
            self.tasks.append(task)

    def load_tasks_from_history(self):
        loaded_tasks = load_history()
        for task in loaded_tasks:
            self.add_task_to_ui(task)
            
            # Immediately mark previously extracted batches as handled so they aren't re-extracted
            if task.status == "Extracted":
                self.extracted_folders.add(task.folder_name)
            # If the app was closed while extracting, reset it to Completed so it can retry properly if needed
            elif task.status == "Extracting...":
                task.status = "Completed"

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

    def paste_from_clipboard(self):
        clipboard = QApplication.clipboard()
        text = clipboard.text()
        if text:
            # Append if there is text, else just set it
            current_text = self.text_links.toPlainText()
            if current_text.strip():
                self.text_links.setText(current_text + "\n" + text)
            else:
                self.text_links.setText(text)

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
            self.add_task_to_ui(task)
            
        self.text_links.clear()

    def toggle_select_all(self):
        self.is_all_selected = not self.is_all_selected
        state = Qt.CheckState.Checked if self.is_all_selected else Qt.CheckState.Unchecked
        
        # Iterate over all top level items and their children
        for i in range(self.tree.topLevelItemCount()):
            batch_item = self.tree.topLevelItem(i)
            batch_item.setCheckState(1, state)
            for j in range(batch_item.childCount()):
                child_item = batch_item.child(j)
                child_item.setCheckState(1, state)
                
        for task in self.tasks:
            task.is_selected = self.is_all_selected

    def handle_item_clicked(self, item, col):
        if col == 1: # Column 1 is now the Checkbox column
            state = item.checkState(1)
            
            # If it's a batch (top-level) item, apply to all children
            if item.parent() is None:
                for i in range(item.childCount()):
                    child = item.child(i)
                    child.setCheckState(1, state)
                    # Update underlying tasks
                    task = next((t for t in self.tasks if t.tree_item == child), None)
                    if task:
                        task.is_selected = (state == Qt.CheckState.Checked)
            else:
                # It's a child item, update its specific task
                task = next((t for t in self.tasks if t.tree_item == item), None)
                if task:
                    task.is_selected = (state == Qt.CheckState.Checked)

    def get_selected_tasks(self):
        # First check explicitly checked boxes
        checked = [t for t in self.tasks if t.tree_item and t.tree_item.checkState(1) == Qt.CheckState.Checked]
        if checked:
            return checked
            
        # If nothing is explicitly checked via checkboxes, fallback to highlighted/selected tree rows
        selected_items = self.tree.selectedItems()
        selected_tasks = []
        for item in selected_items:
            # If a batch parent is highlighted, get all its child tasks
            if item.parent() is None:
                for i in range(item.childCount()):
                    child = item.child(i)
                    task = next((t for t in self.tasks if t.tree_item == child), None)
                    if task and task not in selected_tasks:
                        selected_tasks.append(task)
            else:
                task = next((t for t in self.tasks if t.tree_item == item), None)
                if task and task not in selected_tasks:
                    selected_tasks.append(task)
        return selected_tasks

    def start_downloads(self):
        for task in self.get_selected_tasks():
            if task.status in ("Queued", "Cancelled", "Error", "Paused"):
                task.status = "Pending"
                task.cancel_flag = False
                task.pause_flag = False

    def pause_selected(self):
        for task in self.get_selected_tasks():
            if task.status in ("Downloading", "Pending", "Starting..."):
                task.pause_flag = True
                task.status = "Pausing..." if task.status == "Downloading" else "Paused"

    def cancel_selected(self):
        for task in self.get_selected_tasks():
            if task.status in ("Downloading", "Pending", "Paused", "Starting...", "Queued"):
                task.cancel_flag = True
                task.pause_flag = False
                task.status = "Cancelled"

    def retry_selected(self):
        for task in self.get_selected_tasks():
            if "Error" in task.status:
                task.status = "Pending"
                task.cancel_flag = False
                task.pause_flag = False

    def delete_selected(self):
        tasks_to_delete = self.get_selected_tasks()
        if not tasks_to_delete:
            return
            
        delete_files = False
        
        if not self.settings.get("skip_delete_confirmation", False):
            dialog = QDialog(self)
            dialog.setWindowTitle("Confirm Delete")
            layout = QVBoxLayout(dialog)
            
            label = QLabel(f"Are you sure you want to delete {len(tasks_to_delete)} selected task(s)?")
            layout.addWidget(label)
            
            file_checkbox = QCheckBox("Also delete downloaded files from disk")
            layout.addWidget(file_checkbox)
            
            dont_ask_checkbox = QCheckBox("Don't ask again")
            layout.addWidget(dont_ask_checkbox)
            
            button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No)
            button_box.accepted.connect(dialog.accept)
            button_box.rejected.connect(dialog.reject)
            layout.addWidget(button_box)
            
            if dialog.exec() == QDialog.DialogCode.Accepted:
                delete_files = file_checkbox.isChecked()
                if dont_ask_checkbox.isChecked():
                    self.settings["skip_delete_confirmation"] = True
                    self.skip_delete_checkbox.setChecked(True) if hasattr(self, 'skip_delete_checkbox') else None
                    save_settings(self.settings)
            else:
                return # Cancelled
                
        # Proceed with deletion
        for task in tasks_to_delete:
            # 1. Cancel the task if it's active
            task.cancel_flag = True
            task.status = "Cancelled"
            
            # 2. Delete the physical file if requested
            if delete_files and os.path.exists(task.filepath):
                try:
                    os.remove(task.filepath)
                except Exception as e:
                    print(f"Failed to delete {task.filepath}: {e}")
                    
            # 3. Remove from UI tree
            if task.tree_item:
                parent = task.tree_item.parent()
                if parent:
                    parent.removeChild(task.tree_item)
                    if parent.childCount() == 0:
                        idx = self.tree.indexOfTopLevelItem(parent)
                        if idx >= 0:
                            self.tree.takeTopLevelItem(idx)
                            
            # 4. Remove from tasks list
            if task in self.tasks:
                self.tasks.remove(task)
                
    def clear_finished(self):
        to_remove = [t for t in self.tasks if t.status in ("Completed", "Extracted", "Cancelled")]
        
        for t in to_remove:
            if t.tree_item:
                parent = t.tree_item.parent()
                if parent:
                    parent.removeChild(t.tree_item)
                    # If parent batch is now empty, remove it too
                    if parent.childCount() == 0:
                        idx = self.tree.indexOfTopLevelItem(parent)
                        if idx >= 0:
                            self.tree.takeTopLevelItem(idx)
            self.tasks.remove(t)

    def format_eta(self, seconds):
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

    def update_ui(self):
        global_speed = 0.0
        
        # Update individual tasks
        for task in self.tasks:
            if not task.tree_item:
                continue
            prog_str = f"{task.progress:.1f}%" if task.status not in ("Extracted", "Extracting...", "Extract Error") else "-"
            speed_str = f"{task.speed:.2f} MB/s" if task.status == "Downloading" else "-"
            size_mb = task.total_bytes / (1024*1024)
            dl_mb = task.downloaded_bytes / (1024*1024)
            size_str = f"{dl_mb:.1f} / {size_mb:.1f} MB" if task.total_bytes > 0 else "-"
            
            eta_str = "-"
            if task.status == "Downloading" and task.speed > 0 and task.total_bytes > 0:
                remaining_bytes = task.total_bytes - task.downloaded_bytes
                eta_seconds = remaining_bytes / (task.speed * 1024 * 1024)
                eta_str = self.format_eta(eta_seconds)
            elif task.status in ("Completed", "Extracted", "Extracting..."):
                eta_str = "-"
            
            task.tree_item.setText(2, task.status)
            task.tree_item.setText(3, prog_str)
            task.tree_item.setText(4, speed_str)
            task.tree_item.setText(5, eta_str)
            task.tree_item.setText(6, size_str)
            
            if task.status == "Downloading":
                global_speed += task.speed
                
        self.global_speed_label.setText(f"Global Speed: {global_speed:.2f} MB/s")
            
        # Update top-level batch folders
        for i in range(self.tree.topLevelItemCount()):
            batch_item = self.tree.topLevelItem(i)
            total_dl = 0
            total_size = 0
            total_speed = 0.0
            
            all_completed = True
            any_error = False
            any_downloading = False
            
            child_count = batch_item.childCount()
            if child_count == 0:
                continue
                
            for j in range(child_count):
                child = batch_item.child(j)
                task = next((t for t in self.tasks if t.tree_item == child), None)
                if task:
                    total_dl += task.downloaded_bytes
                    total_size += task.total_bytes
                    total_speed += getattr(task, 'speed', 0.0)
                    
                    if task.status not in ("Completed", "Extracted"):
                        all_completed = False
                    if "Error" in task.status:
                        any_error = True
                    if task.status in ("Downloading", "Starting...", "Pending"):
                        any_downloading = True
                        
            # Determine batch status
            batch_status = "Queued"
            if all_completed:
                # If all are completed but we are extracting, say Extracting...
                if any(t.status == "Extracting..." for t in [next((t for t in self.tasks if t.tree_item == batch_item.child(k)), None) for k in range(batch_item.childCount()) if next((t for t in self.tasks if t.tree_item == batch_item.child(k)), None)]):
                    batch_status = "Extracting..."
                else:
                    batch_status = "Completed"
            elif any_error:
                batch_status = "Contains Errors"
            elif any_downloading:
                batch_status = "Active"
                
            prog = (total_dl / total_size * 100) if total_size > 0 else 0
            prog_str = f"{prog:.1f}%"
            speed_str = f"{total_speed:.2f} MB/s" if total_speed > 0 else "-"
            size_mb = total_size / (1024*1024)
            dl_mb = total_dl / (1024*1024)
            size_str = f"{dl_mb:.1f} / {size_mb:.1f} MB" if total_size > 0 else "-"
            
            eta_str = "-"
            if any_downloading and total_speed > 0 and total_size > 0:
                remaining_bytes = total_size - total_dl
                eta_seconds = remaining_bytes / (total_speed * 1024 * 1024)
                eta_str = self.format_eta(eta_seconds)
            
            batch_item.setText(2, batch_status)
            batch_item.setText(3, prog_str)
            batch_item.setText(4, speed_str)
            batch_item.setText(5, eta_str)
            batch_item.setText(6, size_str)

    def download_manager(self):
        while True:
            active = sum(1 for t in self.tasks if t.status in ("Downloading", "Starting..."))
            if active < self.max_workers:
                for task in self.tasks:
                    if task.status == "Pending":
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
            # Exclude batches that contain errors, cancelled, paused, queued, etc.
            # We ONLY want to trigger extraction if everything is completed or extracted.
            valid_extraction_statuses = {"Completed", "Extracted", "Extracting..."}
            if tasks_in_folder and all(t.status in valid_extraction_statuses for t in tasks_in_folder):
                # If everything is already Extracted, skip
                if all(t.status == "Extracted" for t in tasks_in_folder):
                    self.extracted_folders.add(folder_name)
                    continue
                    
                # If ANY task in this folder is currently Extracting..., don't spawn another thread
                if any(t.status == "Extracting..." for t in tasks_in_folder):
                    continue
                    
                self.extracted_folders.add(folder_name)
                threading.Thread(target=self.extract_folder, args=(tasks_in_folder,), daemon=True).start()

    def extract_folder(self, tasks_in_folder):
        save_dir = tasks_in_folder[0].save_dir
        folder_name = tasks_in_folder[0].folder_name
        
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
                if folder_name in self.extracted_folders:
                    self.extracted_folders.remove(folder_name)
                return
                
            # Locate an available extractor (platform-aware)
            cmd = None
            if sys.platform == 'win32':
                # Windows: prefer installed 7-Zip > WinRAR > bundled 7z.exe
                if hasattr(sys, '_MEIPASS'):
                    bundled_7z = os.path.join(sys._MEIPASS, '7z.exe')
                else:
                    bundled_7z = os.path.join(os.path.dirname(os.path.abspath(__file__)), '7z.exe')
                installed_7z = r"C:\Program Files\7-Zip\7z.exe"
                installed_winrar = r"C:\Program Files\WinRAR\WinRAR.exe"
                if os.path.exists(installed_7z):
                    cmd = [installed_7z, 'x', first_vol, f'-o{save_dir}', '-y']
                elif os.path.exists(installed_winrar):
                    cmd = [installed_winrar, 'x', '-y', first_vol, f'{save_dir}\\']
                elif os.path.exists(bundled_7z):
                    cmd = [bundled_7z, 'x', first_vol, f'-o{save_dir}', '-y']
            else:
                # Linux / macOS: use system 7z from p7zip
                linux_7z = '/usr/bin/7z'
                if os.path.exists(linux_7z):
                    cmd = [linux_7z, 'x', first_vol, f'-o{save_dir}', '-y']
                
            if not cmd:
                for t in tasks_in_folder:
                    t.status = "Extract Error (No extractor found)"
                if folder_name in self.extracted_folders:
                    self.extracted_folders.remove(folder_name)
                return
                
            # Run extraction silently without spawning a console window (Windows only)
            creationflags = 0x08000000 if sys.platform == 'win32' else 0
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
            if folder_name in self.extracted_folders:
                self.extracted_folders.remove(folder_name)
        except Exception as e:
            for t in tasks_in_folder:
                t.status = f"Extract Error"
            if folder_name in self.extracted_folders:
                self.extracted_folders.remove(folder_name)

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
