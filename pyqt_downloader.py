import sys
import os
import time
import threading
import re
import subprocess
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTextEdit, QTableWidget,
    QTableWidgetItem, QHeaderView, QFileDialog, QAbstractItemView,
    QCheckBox
)
from PyQt6.QtCore import Qt, QTimer

import cloudscraper

class DownloadTask:
    def __init__(self, link, save_dir):
        self.link = link.strip()
        self.base_save_dir = save_dir
        
        self.file_id = self.link.split('/')[-1].split('#')[0]
        self.filename = self.link.split('#')[-1] if '#' in self.link else self.file_id
        
        # Calculate smart directory grouping based on prefix
        match = re.search(r'(.*?)(\.part\d+\.rar|\.rar)$', self.filename, re.IGNORECASE)
        if match:
            self.folder_name = match.group(1).strip('._-')
        else:
            self.folder_name = self.filename.rsplit('.', 1)[0]
            
        self.save_dir = os.path.join(self.base_save_dir, self.folder_name)
        self.filepath = os.path.join(self.save_dir, self.filename)
        
        self.status = "Queued"  # Queued, Pending, Starting..., Downloading, Paused, Cancelled, Completed, Extracting..., Extracted, Error
        self.progress = 0.0
        self.speed = 0.0
        self.downloaded_bytes = 0
        self.total_bytes = 0
        
        self.pause_flag = False
        self.cancel_flag = False
        self.row_idx = None
        self.is_selected = False

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FuckingFast Downloader - UI (PyQt6)")
        self.resize(1000, 650)
        
        self.tasks = []
        self.max_workers = 3
        self.scraper = cloudscraper.create_scraper(browser='chrome')
        self.is_all_selected = False
        self.extracted_folders = set()
        
        self.setup_ui()
        
        # Start Background Download Manager
        self.manager_thread = threading.Thread(target=self.download_manager, daemon=True)
        self.manager_thread.start()
        
        # UI Updater Timer
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_ui)
        self.timer.start(500) # update every 500ms

    def setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        
        # 1. Directory Section
        dir_layout = QHBoxLayout()
        dir_layout.addWidget(QLabel("Base Save Directory:"))
        self.dir_input = QLineEdit(os.path.abspath("."))
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
        
        self.start_btn = QPushButton("Start Selected")
        self.start_btn.setStyleSheet("background-color: #2ecc71; color: white; font-weight: bold; padding: 6px;")
        self.start_btn.clicked.connect(self.start_downloads)
        action_layout.addWidget(self.start_btn)
        
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setStyleSheet("background-color: #f39c12; color: white; font-weight: bold; padding: 6px;")
        self.pause_btn.clicked.connect(self.pause_selected)
        action_layout.addWidget(self.pause_btn)
        
        self.resume_btn = QPushButton("Resume")
        self.resume_btn.setStyleSheet("background-color: #3498db; color: white; font-weight: bold; padding: 6px;")
        self.resume_btn.clicked.connect(self.resume_selected)
        action_layout.addWidget(self.resume_btn)
        
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setStyleSheet("background-color: #e74c3c; color: white; font-weight: bold; padding: 6px;")
        self.cancel_btn.clicked.connect(self.cancel_selected)
        action_layout.addWidget(self.cancel_btn)
        
        action_layout.addStretch()
        
        self.extract_checkbox = QCheckBox("Extract after download")
        action_layout.addWidget(self.extract_checkbox)
        
        clear_btn = QPushButton("Clear Completed")
        clear_btn.clicked.connect(self.clear_finished)
        action_layout.addWidget(clear_btn)
        
        main_layout.addLayout(action_layout)

    def browse_dir(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Save Directory", self.dir_input.text())
        if folder:
            self.dir_input.setText(folder)

    def add_links(self):
        text = self.text_links.toPlainText().strip()
        if not text:
            return
            
        links = [line.strip() for line in text.split('\n') if line.strip() and line.startswith('http')]
        save_dir = self.dir_input.text()
        
        for link in links:
            task = DownloadTask(link, save_dir)
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
            if task.status in ("Queued", "Cancelled", "Error"):
                task.status = "Pending"
                task.cancel_flag = False
                task.pause_flag = False

    def pause_selected(self):
        for task in self.get_selected_tasks():
            if task.status in ("Downloading", "Pending", "Starting..."):
                task.pause_flag = True
                task.status = "Pausing..." if task.status == "Downloading" else "Paused"

    def resume_selected(self):
        for task in self.get_selected_tasks():
            if task.status in ("Paused", "Error", "Cancelled"):
                task.pause_flag = False
                task.cancel_flag = False
                task.status = "Pending"

    def cancel_selected(self):
        for task in self.get_selected_tasks():
            if task.status in ("Downloading", "Pending", "Paused", "Starting...", "Queued"):
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
                
            # Detect 7z or WinRAR
            seven_z = r"C:\Program Files\7-Zip\7z.exe"
            winrar = r"C:\Program Files\WinRAR\WinRAR.exe"
            
            cmd = None
            if os.path.exists(seven_z):
                cmd = [seven_z, 'x', first_vol, f'-o{save_dir}', '-y']
            elif os.path.exists(winrar):
                cmd = [winrar, 'x', '-y', first_vol, f'{save_dir}\\']
                
            if not cmd:
                for t in tasks_in_folder:
                    t.status = "Extract Error (Install 7z/WinRAR)"
                return
                
            # Run extraction silently without spawning a console window
            creationflags = 0x08000000 # subprocess.CREATE_NO_WINDOW
            subprocess.run(cmd, check=True, creationflags=creationflags)
            
            for t in tasks_in_folder:
                t.status = "Extracted"
                
        except subprocess.CalledProcessError:
            for t in tasks_in_folder:
                t.status = "Extract Error (Corrupt?)"
        except Exception as e:
            for t in tasks_in_folder:
                t.status = f"Extract Error"

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
