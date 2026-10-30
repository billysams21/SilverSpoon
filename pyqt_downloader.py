import sys
import os
import time
import threading
import subprocess
import logging
import logging.handlers
import tempfile
import zipfile
import urllib.request
from collections import deque

LOG_PATH = os.path.expanduser("~/.silverspoon.log")
# Rotate so the log can't grow without bound: 1 MB per file, 5 backups
# (.silverspoon.log.1 ... .5). delay=True avoids creating an empty log.
logging.basicConfig(
    handlers=[logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=1_000_000, backupCount=5,
        encoding='utf-8', delay=True)],
    level=logging.ERROR,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTextEdit, QTreeWidget,
    QTreeWidgetItem, QHeaderView, QFileDialog, QAbstractItemView,
    QCheckBox, QDialog, QFormLayout, QSpinBox, QDialogButtonBox,
    QMessageBox, QInputDialog, QSplashScreen, QMenu, QStyledItemDelegate,
    QComboBox, QFrame, QGridLayout, QProgressBar, QSizePolicy,
    QDateEdit, QRadioButton, QButtonGroup, QGroupBox
)
from PyQt6.QtGui import (
    QAction, QDesktopServices, QIcon, QPixmap, QColor, QBrush, 
    QKeySequence, QPalette, QPainter, QLinearGradient, QPen, QFont,
    QPainterPath
)
from PyQt6.QtCore import Qt, QTimer, QUrl, QEvent, QRectF, QSize, QPointF, QDate, pyqtSignal, QLockFile
from theme_styles import DARK_THEME_QSS, LIGHT_THEME_QSS

class LiveSpeedGraph(QWidget):
    def __init__(self, max_points=30, parent=None):
        super().__init__(parent)
        self.history = deque([0.0] * max_points, maxlen=max_points)
        self.setFixedHeight(34)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)

    def add_sample(self, speed_mb):
        self.history.append(float(speed_mb))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = self.rect()
        w = float(rect.width())
        h = float(rect.height())

        if w <= 0 or h <= 0:
            return

        window_bg = self.palette().color(QPalette.ColorRole.Window)
        is_dark = window_bg.lightness() < 128

        # Draw subtle frame background
        bg_col = QColor(10, 12, 16, 160) if is_dark else QColor(241, 245, 249, 160)
        border_col = QColor(34, 39, 49, 140) if is_dark else QColor(203, 213, 225, 140)
        painter.setPen(QPen(border_col, 1))
        painter.setBrush(QBrush(bg_col))
        painter.drawRoundedRect(QRectF(0.5, 0.5, w - 1.0, h - 1.0), 2, 2)

        data = list(self.history)
        n = len(data)
        if n < 2:
            return

        max_val = max(data)
        if max_val <= 0.05:
            max_val = 1.0  # baseline scale
        else:
            max_val = max_val * 1.25  # 25% headroom

        top_padding = 4.0
        bottom_padding = 4.0
        draw_h = h - top_padding - bottom_padding

        step_x = (w - 2.0) / (n - 1)
        points = []
        for i, val in enumerate(data):
            px = 1.0 + i * step_x
            ratio = min(1.0, max(0.0, val / max_val))
            py = (h - bottom_padding) - (ratio * draw_h)
            points.append(QPointF(px, py))

        # Filled area underneath the graph line
        fill_path = QPainterPath()
        fill_path.moveTo(points[0].x(), h - 1.0)
        for pt in points:
            fill_path.lineTo(pt)
        fill_path.lineTo(points[-1].x(), h - 1.0)
        fill_path.closeSubpath()

        grad = QLinearGradient(0, 0, 0, h)
        if is_dark:
            grad.setColorAt(0.0, QColor(63, 185, 80, 75))
            grad.setColorAt(1.0, QColor(63, 185, 80, 5))
            line_color = QColor(63, 185, 80, 230)
        else:
            grad.setColorAt(0.0, QColor(26, 127, 55, 60))
            grad.setColorAt(1.0, QColor(26, 127, 55, 5))
            line_color = QColor(26, 127, 55, 230)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(grad))
        painter.drawPath(fill_path)

        # Line on top
        line_path = QPainterPath()
        line_path.moveTo(points[0])
        for pt in points[1:]:
            line_path.lineTo(pt)

        painter.setPen(QPen(line_color, 1.5))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(line_path)


class ScheduleDot(QWidget):
    """Alt C — mini clock icon (20px). Hidden when disabled, hover shows
    the full schedule. Hands point to the window's start time so the icon
    itself is informational at a glance with a crisp, high-visibility outline."""

    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(20, 20)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._armed_text = ""
        self._window_active = False
        self._start_hhmm = ""  # "HH:mm" 24h for hand angles

    def set_state(self, text: str, window_active: bool = False, start_hhmm: str = ""):
        self._armed_text = text or ""
        self._window_active = bool(window_active)
        self._start_hhmm = str(start_hhmm or "")
        armed = bool(self._armed_text)
        self.setVisible(armed)
        if armed:
            # Multi-line hover: schedule is the hero, state + action underneath
            state_line = "Window open • downloading" if self._window_active else "Waiting for window"
            tip = f"{self._armed_text}\n{state_line}\nClick to edit schedule"
            self.setToolTip(tip)
        else:
            self.setToolTip("")
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def paintEvent(self, event):
        if not self._armed_text:
            return
        import math
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        window_bg = self.palette().color(QPalette.ColorRole.Window)
        is_dark = window_bg.lightness() < 128
        rect = self.rect()
        cx = rect.width() / 2.0
        cy = rect.height() / 2.0

        # Outer subtle halo
        halo_col = QColor(63, 185, 80, 35) if self._window_active else QColor(88, 166, 255, 30)
        if not is_dark:
            halo_col = QColor(26, 127, 55, 25) if self._window_active else QColor(9, 105, 218, 20)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(halo_col))
        painter.drawEllipse(QRectF(cx - 9.5, cy - 9.5, 19, 19))

        # Distinct high-contrast outer bezel/outline
        r = 7.2  # dial radius
        face_fill = QColor(13, 17, 23) if is_dark else QColor(255, 255, 255)
        if self._window_active:
            bezel_color = QColor(63, 185, 80) if is_dark else QColor(26, 127, 55)
        else:
            bezel_color = QColor(88, 166, 255) if is_dark else QColor(9, 105, 218)

        painter.setBrush(QBrush(face_fill))
        bezel_pen = QPen(bezel_color, 1.6)
        painter.setPen(bezel_pen)
        painter.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))

        # Ticks at 12/3/6/9 — high-contrast crisp marks
        tick_col = QColor(139, 148, 158, 200) if is_dark else QColor(101, 109, 118, 190)
        painter.setPen(QPen(tick_col, 1))
        # 12 o'clock
        painter.drawLine(QPointF(cx, cy - r + 1.2), QPointF(cx, cy - r + 3.0))
        # 3
        painter.drawLine(QPointF(cx + r - 1.2, cy), QPointF(cx + r - 3.0, cy))
        # 6
        painter.drawLine(QPointF(cx, cy + r - 1.2), QPointF(cx, cy + r - 3.0))
        # 9
        painter.drawLine(QPointF(cx - r + 1.2, cy), QPointF(cx - r + 3.0, cy))

        # Resolve hand angles from start time; fallback to 10:10 (readable V)
        try:
            hh_s, mm_s = self._start_hhmm.split(":")
            hh, mm = int(hh_s), int(mm_s)
        except Exception:
            hh, mm = 10, 10
            if self._start_hhmm == "" and self._armed_text:
                pass
        hh = max(0, min(23, hh))
        mm = max(0, min(59, mm))
        minute_angle_deg = mm * 6.0  # 0 at 12, clockwise
        hour_angle_deg = (hh % 12) * 30.0 + mm * 0.5

        def polar(angle_deg, length):
            a = math.radians(angle_deg)
            return cx + math.sin(a) * length, cy - math.cos(a) * length

        hand_col = QColor(240, 246, 252) if is_dark else QColor(31, 35, 40)
        hand_active = QColor(63, 185, 80) if is_dark else QColor(26, 127, 55)

        # Hour hand — bold, shorter
        hx, hy = polar(hour_angle_deg, 3.8)
        _pen = QPen(hand_active if self._window_active else hand_col, 1.6)
        _pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(_pen)
        painter.drawLine(QPointF(cx, cy), QPointF(hx, hy))

        # Minute hand — longer, crisp
        mx, my = polar(minute_angle_deg, 5.2)
        m_col = hand_col if not self._window_active else (QColor(180, 240, 195) if is_dark else QColor(35, 90, 45))
        _pen2 = QPen(m_col, 1.2)
        _pen2.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(_pen2)
        painter.drawLine(QPointF(cx, cy), QPointF(mx, my))

        # Center pin
        pin_fill = hand_active if self._window_active else bezel_color
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(pin_fill))
        painter.drawEllipse(QRectF(cx - 1.3, cy - 1.3, 2.6, 2.6))
        if is_dark:
            painter.setBrush(QBrush(QColor(255, 255, 255, 120)))
            painter.drawEllipse(QRectF(cx - 0.6, cy - 0.8, 1.0, 1.0))


class ModernTaskDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)

    def paint(self, painter: QPainter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        item = self.parent().itemFromIndex(index)
        col = index.column()
        rect = option.rect

        # Retrieve task progress and status data
        progress = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        status = item.data(1, Qt.ItemDataRole.UserRole) if item else ""
        if status is None:
            status = item.text(2) if item else ""

        # Column 0: Clean native render (no background tinting for maximum text clarity)
        if col == 0:
            pass

        # Column 2: Status Tag (Crisp subtle border, minimal rounding, theme-aware contrast)
        elif col == 2:
            status_text = item.text(2) if item else ""
            if status_text:
                is_error = "Error" in status_text or status_text == "Contains Errors" or status_text == "CAPTCHA Timeout"
                is_done = status_text in ("Completed", "Extracted")
                is_active = status_text in ("Downloading", "Active", "Extracting...", "Solving CAPTCHA...", "Waiting for you to solve CAPTCHA", "Starting...")
                is_paused = status_text in ("Paused", "Pausing...", "Cancelled", "Extract Cancelled")

                # Detect if the app is currently running in dark or light mode based on window palette
                window_bg = option.palette.color(QPalette.ColorRole.Window)
                is_dark = window_bg.lightness() < 128

                if is_dark:
                    if is_error:
                        bg_color = QColor(239, 68, 68, 30)
                        border_color = QColor(239, 68, 68, 140)
                        text_color = QColor(252, 165, 165)
                    elif is_done:
                        bg_color = QColor(34, 197, 94, 25)
                        border_color = QColor(34, 197, 94, 140)
                        text_color = QColor(134, 239, 172)
                    elif is_active:
                        bg_color = QColor(14, 165, 233, 25)
                        border_color = QColor(14, 165, 233, 140)
                        text_color = QColor(125, 211, 252)
                    elif is_paused:
                        bg_color = QColor(234, 179, 8, 20)
                        border_color = QColor(234, 179, 8, 120)
                        text_color = QColor(253, 224, 71)
                    else:
                        bg_color = QColor(100, 116, 139, 20)
                        border_color = QColor(100, 116, 139, 80)
                        text_color = QColor(203, 213, 225)
                else:
                    # Light mode high-contrast text and solid legible backgrounds
                    if is_error:
                        bg_color = QColor(254, 242, 242)
                        border_color = QColor(248, 113, 113)
                        text_color = QColor(153, 27, 27)
                    elif is_done:
                        bg_color = QColor(240, 253, 244)
                        border_color = QColor(74, 222, 128)
                        text_color = QColor(22, 101, 52)
                    elif is_active:
                        bg_color = QColor(240, 249, 255)
                        border_color = QColor(56, 189, 248)
                        text_color = QColor(7, 89, 133)
                    elif is_paused:
                        bg_color = QColor(254, 252, 232)
                        border_color = QColor(250, 204, 21)
                        text_color = QColor(133, 77, 14)
                    else:
                        bg_color = QColor(248, 250, 252)
                        border_color = QColor(203, 213, 225)
                        text_color = QColor(51, 65, 85)

                tag_height = 18
                tag_y = rect.y() + (rect.height() - tag_height) / 2
                
                # Measure text width
                font = painter.font()
                font.setPointSize(9)
                font.setBold(True)
                painter.setFont(font)
                metrics = painter.fontMetrics()
                text_w = metrics.horizontalAdvance(status_text)

                # Clamp the pill to the column so it never overflows into the
                # next column; elide the label (e.g. "Solving CAPTCHA...") if it
                # doesn't fit the available width.
                max_tag_w = max(24, rect.width() - 6)
                tag_w = min(text_w + 12, max_tag_w)
                tag_rect = QRectF(rect.x() + 2, tag_y, tag_w, tag_height)

                painter.setBrush(QBrush(bg_color))
                painter.setPen(QPen(border_color, 1))
                painter.drawRoundedRect(tag_rect, 2, 2)

                elided = metrics.elidedText(
                    status_text, Qt.TextElideMode.ElideRight, int(tag_w) - 8)
                painter.setPen(text_color)
                painter.drawText(tag_rect, Qt.AlignmentFlag.AlignCenter, elided)
                painter.restore()
                return

        # Column 3: Progress Bar (Linear precision bar)
        elif col == 3:
            if progress is not None and isinstance(progress, (int, float)):
                bar_h = 14
                bar_y = rect.y() + (rect.height() - bar_h) / 2
                bar_w = rect.width() - 8
                bar_rect = QRectF(rect.x() + 4, bar_y, bar_w, bar_h)

                window_bg = option.palette.color(QPalette.ColorRole.Window)
                is_dark = window_bg.lightness() < 128

                # Background trough
                if is_dark:
                    trough_color = QColor(28, 34, 44, 140)
                    trough_border = QColor(45, 55, 70, 120)
                else:
                    trough_color = QColor(226, 232, 240)
                    trough_border = QColor(203, 213, 225)

                painter.setPen(QPen(trough_border, 1))
                painter.setBrush(QBrush(trough_color))
                painter.drawRoundedRect(bar_rect, 2, 2)

                # Active progress fill
                fill_w = max(0, bar_w * (min(100.0, max(0.0, float(progress))) / 100.0))
                if fill_w > 0:
                    fill_rect = QRectF(rect.x() + 4, bar_y, fill_w, bar_h)
                    
                    is_error = "Error" in status or status == "Contains Errors"
                    is_done = status in ("Completed", "Extracted", "Extract Cancelled")
                    
                    # Progress bar stays standard neutral/blue fill, or emerald on complete;
                    # violet while extracting so it reads differently from a download.
                    if status == "Extracting...":
                        g_start, g_end = (QColor("#a78bfa"), QColor("#6d28d9")) if is_dark else (QColor("#7c3aed"), QColor("#5b21b6"))
                    elif is_done:
                        g_start, g_end = (QColor("#22c55e"), QColor("#15803d")) if is_dark else (QColor("#16a34a"), QColor("#15803d"))
                    elif is_error:
                        # Muted slate/amber fill on error so it doesn't scream red alongside the red error tag
                        g_start, g_end = (QColor("#64748b"), QColor("#475569")) if is_dark else (QColor("#94a3b8"), QColor("#64748b"))
                    else:
                        g_start, g_end = (QColor("#38bdf8"), QColor("#1d4ed8")) if is_dark else (QColor("#0284c7"), QColor("#0369a1"))

                    p_grad = QLinearGradient(fill_rect.topLeft(), fill_rect.bottomRight())
                    p_grad.setColorAt(0, g_start)
                    p_grad.setColorAt(1, g_end)

                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QBrush(p_grad))
                    painter.drawRoundedRect(fill_rect, 2, 2)

                # Progress text
                if status == "Extracting...":
                    # 0 until the extractor reports (WinRAR never does).
                    prog_str = f"{progress:.1f}%" if progress > 0 else "Extracting..."
                else:
                    prog_str = f"{progress:.1f}%" if status not in ("Extracted", "Extract Error") else "--"
                font = painter.font()
                font.setPointSize(8)
                font.setBold(True)
                painter.setFont(font)
                
                # Text contrast logic based on fill width
                if fill_w > bar_w * 0.4:
                    text_color = QColor("#ffffff")
                else:
                    text_color = QColor("#ffffff") if is_dark else QColor("#1e293b")
                    
                painter.setPen(text_color)
                painter.drawText(bar_rect, Qt.AlignmentFlag.AlignCenter, prog_str)
                painter.restore()
                return

        painter.restore()
        super().paint(painter, option, index)

ProgressBarDelegate = ModernTaskDelegate

from update_logic import UpdateCheckerThread
import datetime as _dt
import scheduler as offpeak
from ui_style import button_style
from engine import (
    DownloadEngine, DownloadTask, CURRENT_VERSION, load_settings, save_settings,
    get_settings_path, parse_links, suggest_folder,
)

GITHUB_REPO = "billysams21/SilverSpoon"

def apply_theme(app, theme_name):
    if theme_name == "Dark":
        app.setStyle("Fusion")
        dark_palette = QPalette()
        dark_palette.setColor(QPalette.ColorRole.Window, QColor("#0f1115"))
        dark_palette.setColor(QPalette.ColorRole.WindowText, QColor("#e6edf3"))
        dark_palette.setColor(QPalette.ColorRole.Base, QColor("#0d0f14"))
        dark_palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#161922"))
        dark_palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#1c212c"))
        dark_palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#f0f6fc"))
        dark_palette.setColor(QPalette.ColorRole.Text, QColor("#e6edf3"))
        dark_palette.setColor(QPalette.ColorRole.Button, QColor("#21262d"))
        dark_palette.setColor(QPalette.ColorRole.ButtonText, QColor("#c9d1d9"))
        dark_palette.setColor(QPalette.ColorRole.BrightText, QColor("#ff7b72"))
        dark_palette.setColor(QPalette.ColorRole.Link, QColor("#58a6ff"))
        dark_palette.setColor(QPalette.ColorRole.Highlight, QColor("#1f6feb"))
        dark_palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
        app.setPalette(dark_palette)
        app.setStyleSheet(DARK_THEME_QSS)
    else:
        app.setStyle("windowsvista" if sys.platform == "win32" else "Fusion")
        light_palette = QPalette()
        light_palette.setColor(QPalette.ColorRole.Window, QColor("#f6f8fa"))
        light_palette.setColor(QPalette.ColorRole.WindowText, QColor("#1f2328"))
        light_palette.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
        light_palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#f6f8fa"))
        light_palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#24292f"))
        light_palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#ffffff"))
        light_palette.setColor(QPalette.ColorRole.Text, QColor("#1f2328"))
        light_palette.setColor(QPalette.ColorRole.Button, QColor("#f6f8fa"))
        light_palette.setColor(QPalette.ColorRole.ButtonText, QColor("#24292f"))
        light_palette.setColor(QPalette.ColorRole.BrightText, QColor("#cf222e"))
        light_palette.setColor(QPalette.ColorRole.Link, QColor("#0969da"))
        light_palette.setColor(QPalette.ColorRole.Highlight, QColor("#0969da"))
        light_palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
        app.setPalette(light_palette)
        app.setStyleSheet(LIGHT_THEME_QSS)

class WarningDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Welcome to SilverSpoon!")
        self.setMinimumWidth(480)
        self.settings = settings
        
        layout = QVBoxLayout(self)
        
        # Shortcuts Section
        shortcuts_label = QLabel("<b>Keyboard Shortcuts:</b>")
        layout.addWidget(shortcuts_label)
        
        shortcuts_text = (
            "<ul>"
            "<li><b>[S] or [Space]</b>: Start / Resume selected downloads</li>"
            "<li><b>[P] or [Space]</b>: Pause selected downloads</li>"
            "<li><b>[C]</b>: Cancel selected downloads</li>"
            "<li><b>[R]</b>: Retry failed downloads</li>"
            "<li><b>[F]</b>: Force Redownload selected tasks</li>"
            "<li><b>[Delete] or [Backspace]</b>: Delete selected tasks</li>"
            "</ul>"
        )
        shortcuts_display = QLabel(shortcuts_text)
        shortcuts_display.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(shortcuts_display)
        
        # Tips Section
        tips_label = QLabel("<b>Tips & Troubleshooting:</b>")
        layout.addWidget(tips_label)
        
        tips_box = QLabel(
            "• <b>Slow Speeds:</b> If your download speed is too slow, try changing your DNS (e.g., Cloudflare 1.1.1.1 or Google 8.8.8.8) or enabling a VPN.<br>"
            "• <b>CAPTCHA:</b> Cloudflare checks are solved automatically in the background. If a browser window pops up asking you to verify you're human, complete it and the download will continue."
        )
        tips_box.setTextFormat(Qt.TextFormat.RichText)
        tips_box.setWordWrap(True)
        tips_box.setStyleSheet("padding: 8px 10px; border: 1px solid #388bfd; border-radius: 6px; background-color: rgba(56, 139, 253, 0.1); font-size: 12px; line-height: 1.4;")
        layout.addWidget(tips_box)
        
        # Don't show again checkbox
        self.dont_show_checkbox = QCheckBox("Don't show this again")
        layout.addWidget(self.dont_show_checkbox)
        
        # OK Button
        btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        btn_box.accepted.connect(self.accept)
        layout.addWidget(btn_box)

    def accept(self):
        if self.dont_show_checkbox.isChecked():
            self.settings["show_warning_dialog"] = False
        super().accept()

class SettingsDialog(QDialog):
    def __init__(self, current_settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(400)
        
        self.current_settings = current_settings
        
        layout = QFormLayout(self)
        
        # Application Theme
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["Dark", "Light"])
        current_theme = self.current_settings.get("theme", "Dark")
        self.theme_combo.setCurrentText(current_theme)
        layout.addRow("Application Theme:", self.theme_combo)
        
        # Save Directory
        dir_layout = QHBoxLayout()
        default_dir = self.current_settings.get("default_save_dir", os.path.join(os.path.expanduser("~"), "Downloads"))
        self.dir_input = QLineEdit(default_dir)
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

        # Connections per download (segmented direct downloads; 1 = off)
        self.connections_spinbox = QSpinBox()
        self.connections_spinbox.setRange(1, 16)
        self.connections_spinbox.setValue(self.current_settings.get("max_connections", 4))
        self.connections_spinbox.setToolTip(
            "Parallel connections per file for direct links (FileKeeper and other "
            "plain download links). 1 turns multi-connection downloads off.")
        layout.addRow("Connections per Download:", self.connections_spinbox)
        
        # Bandwidth Limit
        self.bandwidth_spinbox = QSpinBox()
        self.bandwidth_spinbox.setRange(0, 1000) # 0 means unlimited
        self.bandwidth_spinbox.setSuffix(" MB/s")
        self.bandwidth_spinbox.setSpecialValueText("Unlimited")
        self.bandwidth_spinbox.setValue(self.current_settings.get("bandwidth_limit", 0))
        layout.addRow("Global Bandwidth Limit:", self.bandwidth_spinbox)
        
        # CAPTCHA Timeout
        self.captcha_spinbox = QSpinBox()
        self.captcha_spinbox.setRange(5, 120)
        self.captcha_spinbox.setSuffix(" seconds")
        self.captcha_spinbox.setValue(self.current_settings.get("captcha_timeout", 10))
        layout.addRow("CAPTCHA Solve Timeout:", self.captcha_spinbox)

        # The CAPTCHA browser runs off-screen unless asked to show it
        self.show_solver_checkbox = QCheckBox()
        self.show_solver_checkbox.setChecked(self.current_settings.get("show_solver_browser", False))
        self.show_solver_checkbox.setToolTip(
            "Show the browser window that solves Cloudflare checks for FuckingFast and "
            "DataNodes. Off: it runs off-screen. Applies the next time it starts.")
        layout.addRow("Show the CAPTCHA browser window:", self.show_solver_checkbox)
        
        # Extract Option
        self.extract_checkbox = QCheckBox()
        self.extract_checkbox.setChecked(self.current_settings.get("extract_after_download", False))
        layout.addRow("Extract after download by default:", self.extract_checkbox)
        
        # Auto-retry Errors Option
        self.auto_retry_checkbox = QCheckBox()
        self.auto_retry_checkbox.setChecked(self.current_settings.get("auto_retry_errors", False))
        layout.addRow("Automatically retry failed downloads (up to 3 times):", self.auto_retry_checkbox)
        
        # Skip Delete Confirmation Option
        self.skip_delete_checkbox = QCheckBox()
        self.skip_delete_checkbox.setChecked(self.current_settings.get("skip_delete_confirmation", False))
        layout.addRow("Skip delete confirmation:", self.skip_delete_checkbox)
        
        # Buttons
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.reset_btn = button_box.addButton("Reset Defaults", QDialogButtonBox.ButtonRole.ResetRole)
        self.reset_btn.clicked.connect(self.reset_to_defaults)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addRow(button_box)

    def browse_dir(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Save Directory", self.dir_input.text())
        if folder:
            self.dir_input.setText(os.path.abspath(folder))

    def reset_to_defaults(self):
        reply = QMessageBox.question(
            self, 'Confirm Reset', 
            "Are you sure you want to reset all settings to their default values? (Includes showing warnings and UI sizes)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            if sys.platform == "win32":
                default_dir = os.path.join(os.path.expanduser("~"), "Downloads")
            else:
                default_dir = os.path.abspath(".")
            
            self.dir_input.setText(default_dir)
            self.theme_combo.setCurrentText("Dark")
            self.workers_spinbox.setValue(3)
            self.connections_spinbox.setValue(4)
            self.bandwidth_spinbox.setValue(0)
            self.captcha_spinbox.setValue(10)
            self.show_solver_checkbox.setChecked(False)
            self.extract_checkbox.setChecked(False)
            self.auto_retry_checkbox.setChecked(False)
            self.skip_delete_checkbox.setChecked(False)
            
            # Reset background invisible settings as well
            self.current_settings["column_widths"] = {}
            self.current_settings["show_warning_dialog"] = True

    def get_updated_settings(self):
        return {
            "theme": self.theme_combo.currentText(),
            "default_save_dir": self.dir_input.text(),
            "max_workers": self.workers_spinbox.value(),
            "max_connections": self.connections_spinbox.value(),
            "bandwidth_limit": self.bandwidth_spinbox.value(),
            "captcha_timeout": self.captcha_spinbox.value(),
            "show_solver_browser": self.show_solver_checkbox.isChecked(),
            "extract_after_download": self.extract_checkbox.isChecked(),
            "auto_retry_errors": self.auto_retry_checkbox.isChecked(),
            "skip_delete_confirmation": self.skip_delete_checkbox.isChecked(),
            "column_widths": self.current_settings.get("column_widths", {}),
            "show_warning_dialog": self.current_settings.get("show_warning_dialog", True),
            "last_update_check": self.current_settings.get("last_update_check", 0.0)
        }


class TimePicker(QWidget):
    """Hour / minute / AM-PM dropdowns. Reads and writes 24-hour 'HH:mm'
    internally so the stored schedule format is unchanged; the user sees a
    clear 12-hour time with an explicit AM/PM instead of a bare spinbox."""

    def __init__(self, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.hour = QComboBox()
        self.hour.addItems([f"{h:02d}" for h in range(1, 13)])
        self.minute = QComboBox()
        self.minute.addItems([f"{m:02d}" for m in range(60)])
        self.ampm = QComboBox()
        self.ampm.addItems(["AM", "PM"])
        row.addWidget(self.hour)
        row.addWidget(QLabel(":"))
        row.addWidget(self.minute)
        row.addWidget(self.ampm)
        row.addStretch()

    def set_hhmm(self, value):
        h12, m, ampm = offpeak.split_12h(value)
        self.hour.setCurrentText(f"{h12:02d}")
        self.minute.setCurrentText(f"{m:02d}")
        self.ampm.setCurrentText(ampm)

    def get_hhmm(self):
        return offpeak.join_24h(int(self.hour.currentText()),
                                int(self.minute.currentText()),
                                self.ampm.currentText())


class DownloadSchedulerDialog(QDialog):
    """Configure a recurring (or one-off) download window, either for the whole
    queue or for a specific set of selected downloads (scope_label)."""

    def __init__(self, schedule, scope_label="entire queue", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Download Scheduler")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)

        scope = QLabel(f"Scheduling: <b>{scope_label}</b>")
        layout.addWidget(scope)

        # --- Window times ---
        times_box = QGroupBox("Window")
        times_form = QFormLayout(times_box)
        self.start_edit = TimePicker()
        self.start_edit.set_hhmm(schedule.get("start", "02:00"))
        self.end_edit = TimePicker()
        self.end_edit.set_hhmm(schedule.get("end", "06:00"))
        times_form.addRow("Start:", self.start_edit)
        times_form.addRow("End:", self.end_edit)
        times_form.addRow(QLabel("<i>End before start = window crosses midnight.</i>"))
        layout.addWidget(times_box)

        # --- Recurrence ---
        rec_box = QGroupBox("Recurrence")
        rec_layout = QVBoxLayout(rec_box)
        self.rec_group = QButtonGroup(self)
        self.weekly_radio = QRadioButton("Repeat weekly on:")
        self.once_radio = QRadioButton("Run once on:")
        self.rec_group.addButton(self.weekly_radio)
        self.rec_group.addButton(self.once_radio)
        rec_layout.addWidget(self.weekly_radio)

        days_row = QHBoxLayout()
        self.day_checks = []
        active_days = set(schedule.get("days", list(range(7))))
        for i, name in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]):
            cb = QCheckBox(name)
            cb.setChecked(i in active_days)
            self.day_checks.append(cb)
            days_row.addWidget(cb)
        rec_layout.addLayout(days_row)

        once_row = QHBoxLayout()
        once_row.addWidget(self.once_radio)
        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        d = schedule.get("date") or _dt.date.today().isoformat()
        self.date_edit.setDate(QDate.fromString(d, "yyyy-MM-dd"))
        once_row.addWidget(self.date_edit)
        once_row.addStretch()
        rec_layout.addLayout(once_row)
        layout.addWidget(rec_box)

        if schedule.get("recurrence") == "once":
            self.once_radio.setChecked(True)
        else:
            self.weekly_radio.setChecked(True)
        self.rec_group.buttonToggled.connect(self._sync_recurrence_enabled)
        self._sync_recurrence_enabled()

        # --- Power / behaviour ---
        self.wake_cb = QCheckBox("Wake the computer to run downloads (Windows only)")
        self.wake_cb.setChecked(schedule.get("wake_timer", False))
        self.keep_awake_cb = QCheckBox("Keep the computer awake while the window is open")
        self.keep_awake_cb.setChecked(schedule.get("keep_awake", True))
        layout.addWidget(self.wake_cb)
        layout.addWidget(self.keep_awake_cb)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_btn.setText("Add Schedule")
        self.remove_btn = buttons.addButton("Remove Schedule", QDialogButtonBox.ButtonRole.DestructiveRole)
        self.remove_btn.clicked.connect(self._remove_schedule)
        self.remove_btn.setVisible(schedule.get("enabled", False))
        self.is_removed = False
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _remove_schedule(self):
        self.is_removed = True
        self.accept()

    def accept(self):
        if getattr(self, "is_removed", False):
            super().accept()
            return

        # Guard: a weekly schedule with no weekday ticked would never fire.
        if (self.weekly_radio.isChecked()
                and not any(cb.isChecked() for cb in self.day_checks)):
            QMessageBox.warning(self, "No days selected",
                                "Pick at least one day, or choose Run once.")
            return

        # Guard: a one-off schedule with a date that is completely in the past.
        if self.once_radio.isChecked():
            start_date = self.date_edit.date().toPyDate()
            start_t = offpeak.parse_hhmm(self.start_edit.get_hhmm())
            end_t = offpeak.parse_hhmm(self.end_edit.get_hhmm())
            end_date = start_date + _dt.timedelta(days=1) if end_t <= start_t else start_date
            end_dt = _dt.datetime.combine(end_date, end_t)
            if _dt.datetime.now() >= end_dt:
                QMessageBox.warning(self, "Invalid Date",
                                    "The selected one-off schedule window has already passed.\n"
                                    "Please select a current or future date.")
                return
        super().accept()

    def _sync_recurrence_enabled(self, *args):
        weekly = self.weekly_radio.isChecked()
        for cb in self.day_checks:
            cb.setEnabled(weekly)
        self.date_edit.setEnabled(not weekly)

    def get_schedule(self):
        if getattr(self, "is_removed", False):
            return {
                "enabled": False,
                "start": self.start_edit.get_hhmm(),
                "end": self.end_edit.get_hhmm(),
                "recurrence": "once" if self.once_radio.isChecked() else "weekly",
                "days": [i for i, cb in enumerate(self.day_checks) if cb.isChecked()],
                "date": self.date_edit.date().toString("yyyy-MM-dd"),
                "wake_timer": self.wake_cb.isChecked(),
                "keep_awake": self.keep_awake_cb.isChecked(),
            }
        return {
            "enabled": True,
            "start": self.start_edit.get_hhmm(),
            "end": self.end_edit.get_hhmm(),
            "recurrence": "once" if self.once_radio.isChecked() else "weekly",
            "days": [i for i, cb in enumerate(self.day_checks) if cb.isChecked()],
            "date": self.date_edit.date().toString("yyyy-MM-dd"),
            "wake_timer": self.wake_cb.isChecked(),
            "keep_awake": self.keep_awake_cb.isChecked(),
        }

class MainWindow(QMainWindow):
    # Emitted from the connectivity-probe thread once a window-open probe
    # succeeds; carries `now` and is handled on the GUI thread.
    _offpeak_open_ready = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"SilverSpoon {CURRENT_VERSION}")
        self.resize(1000, 650)
        
        if hasattr(sys, '_MEIPASS'):
            self.base_dir = sys._MEIPASS
        else:
            self.base_dir = os.path.dirname(os.path.abspath(__file__))
            
        icon_path = os.path.join(self.base_dir, 'SilverSpoon.ico')
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        
        self.engine = DownloadEngine(autostart=False)
        # The window reads the engine's live queue and settings directly.
        self.settings = self.engine.settings
        self.tasks = self.engine.tasks
        self.is_all_selected = False
        self._last_event_id = 0

        self.setup_ui()
        self.load_tasks_from_history()

        if self.settings.get("show_warning_dialog", True):
            QTimer.singleShot(100, self.show_warning_dialog)

        if sys.platform == "win32" and hasattr(sys, 'frozen'):
            self.update_checker = UpdateCheckerThread(CURRENT_VERSION, GITHUB_REPO, get_settings_path())
            self.update_checker.update_available.connect(self.prompt_update)
            self.update_checker.check_finished.connect(self.update_last_check_time)
            self.update_checker.start()

        self.timer = QTimer()
        self.timer.timeout.connect(self.update_ui)
        self.timer.start(500)

        self._refresh_schedule_indicator()
        self._refresh_wake_task()

        # Offer to finish installing an update that was deferred to "next open".
        QTimer.singleShot(1200, self._check_pending_update)

        # Download manager and off-peak scheduler threads.
        self.engine.start()

    @property
    def schedule(self):
        return self.engine.schedule

    def closeEvent(self, event):
        col_widths = {}
        for i in range(self.tree.columnCount()):
            col_widths[str(i)] = self.tree.columnWidth(i)
        self.settings["column_widths"] = col_widths
        self.engine.stop()   # saves history and settings, stops the solver
        event.accept()

    def setup_ui(self):
        menu_bar = self.menuBar()

        file_menu = menu_bar.addMenu("&File")
        
        import_action = QAction("&Import Links from File...", self)
        import_action.triggered.connect(self.import_links_from_file)
        file_menu.addAction(import_action)
        
        settings_action = QAction("&Settings", self)
        settings_action.triggered.connect(self.open_settings_dialog)
        file_menu.addAction(settings_action)

        schedule_action = QAction("&Download Scheduler...", self)
        schedule_action.triggered.connect(self.open_queue_scheduler)
        file_menu.addAction(schedule_action)

        file_menu.addSeparator()

        exit_action = QAction("&Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        view_menu = menu_bar.addMenu("&View")
        
        self.theme_menu = view_menu.addMenu("&Theme")
        self.theme_dark_action = QAction("Dark Theme", self, checkable=True)
        self.theme_dark_action.triggered.connect(lambda: self.switch_theme("Dark"))
        self.theme_light_action = QAction("Light Theme", self, checkable=True)
        self.theme_light_action.triggered.connect(lambda: self.switch_theme("Light"))
        self.theme_menu.addAction(self.theme_dark_action)
        self.theme_menu.addAction(self.theme_light_action)
        
        current_theme = self.settings.get("theme", "Dark")
        if current_theme == "Dark":
            self.theme_dark_action.setChecked(True)
        else:
            self.theme_light_action.setChecked(True)

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
        
        welcome_action = QAction("&Welcome", self)
        welcome_action.triggered.connect(self.show_warning_dialog_manual)
        help_menu.addAction(welcome_action)
        
        check_update_action = QAction("Check for &Updates...", self)
        check_update_action.triggered.connect(self.manual_update_check)
        help_menu.addAction(check_update_action)

        about_menu = menu_bar.addMenu("&About")
        
        about_action = QAction("&About SilverSpoon", self)
        about_action.triggered.connect(self.show_about_dialog)
        about_menu.addAction(about_action)
        
        donate_action = QAction("&Donate", self)
        donate_action.triggered.connect(self.open_donate_link)
        about_menu.addAction(donate_action)
        
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(10, 2, 10, 8)
        main_layout.setSpacing(6)

        # 1. TOP BENTO GRID: INGESTION HERO & STATUS
        top_grid = QHBoxLayout()
        top_grid.setSpacing(8)

        # Left Bento Card: Target Directory & Quick Ingest
        left_card = QFrame()
        left_card.setObjectName("bentoCard")
        left_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        left_layout = QVBoxLayout(left_card)
        left_layout.setContentsMargins(10, 6, 10, 6)
        left_layout.setSpacing(6)

        left_header = QHBoxLayout()
        left_header.setSpacing(6)
        left_title = QLabel("DOWNLOAD QUEUE INGEST")
        left_title.setObjectName("sectionTitle")
        left_header.addWidget(left_title)
        left_header.addStretch()

        paste_btn = QPushButton("Paste Clipboard")
        paste_btn.clicked.connect(self.paste_from_clipboard)
        left_header.addWidget(paste_btn)
        left_layout.addLayout(left_header)

        self.text_links = QTextEdit()
        self.text_links.setPlaceholderText("Paste FuckingFast / DataNodes / FileKeeper / BZZHR links or any direct download URLs here...")
        self.text_links.setAcceptRichText(False)
        self.text_links.setFixedHeight(77)
        self.text_links.installEventFilter(self)
        left_layout.addWidget(self.text_links)

        dir_row = QHBoxLayout()
        dir_row.setSpacing(6)
        dir_label = QLabel("Save To:")
        dir_label.setObjectName("statLabel")
        dir_row.addWidget(dir_label)

        default_dir = self.settings.get("default_save_dir", os.path.join(os.path.expanduser("~"), "Downloads"))
        self.dir_input = QLineEdit(default_dir)
        dir_row.addWidget(self.dir_input)

        browse_btn = QPushButton("Browse")
        browse_btn.clicked.connect(self.browse_dir)
        dir_row.addWidget(browse_btn)

        add_btn = QPushButton("Add to Queue")
        add_btn.setObjectName("primaryBtn")
        add_btn.clicked.connect(self.add_links)
        dir_row.addWidget(add_btn)

        left_layout.addLayout(dir_row)
        top_grid.addWidget(left_card, stretch=65)

        # Right Bento Card: Global Status & Stats
        right_card = QFrame()
        right_card.setObjectName("statusCard")
        right_layout = QVBoxLayout(right_card)
        right_layout.setContentsMargins(10, 6, 10, 6)
        right_layout.setSpacing(4)

        status_header = QHBoxLayout()
        status_header.setSpacing(6)
        right_title = QLabel("LIVE STATUS")
        right_title.setObjectName("sectionTitle")
        status_header.addWidget(right_title)

        self.schedule_dot = ScheduleDot()
        self.schedule_dot.clicked.connect(self.open_queue_scheduler)
        self.schedule_dot.setVisible(False)
        status_header.addWidget(self.schedule_dot)

        status_header.addStretch()

        self.global_speed_label = QLabel("0.00 MB/s")
        self.global_speed_label.setObjectName("speedDisplay")
        status_header.addWidget(self.global_speed_label)
        right_layout.addLayout(status_header)

        self.speed_graph = LiveSpeedGraph(max_points=35)
        self.speed_graph.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        right_layout.addWidget(self.speed_graph, stretch=1)

        stats_grid = QGridLayout()
        stats_grid.setContentsMargins(0, 2, 0, 0)
        stats_grid.setHorizontalSpacing(10)
        stats_grid.setVerticalSpacing(1)

        self.stat_active_val = QLabel("0")
        self.stat_active_val.setObjectName("statValue")
        stat_active_lbl = QLabel("Active")
        stat_active_lbl.setObjectName("statLabel")
        stats_grid.addWidget(self.stat_active_val, 0, 0)
        stats_grid.addWidget(stat_active_lbl, 1, 0)

        self.stat_queued_val = QLabel("0")
        self.stat_queued_val.setObjectName("statValue")
        stat_queued_lbl = QLabel("Queued")
        stat_queued_lbl.setObjectName("statLabel")
        stats_grid.addWidget(self.stat_queued_val, 0, 1)
        stats_grid.addWidget(stat_queued_lbl, 1, 1)

        self.stat_done_val = QLabel("0")
        self.stat_done_val.setObjectName("statValue")
        stat_done_lbl = QLabel("Done")
        stat_done_lbl.setObjectName("statLabel")
        stats_grid.addWidget(self.stat_done_val, 0, 2)
        stats_grid.addWidget(stat_done_lbl, 1, 2)

        right_layout.addLayout(stats_grid)
        top_grid.addWidget(right_card, stretch=35)

        main_layout.addLayout(top_grid, stretch=0)

        # 2. MAIN QUEUE TREE
        self.tree = QTreeWidget()
        self.tree.setColumnCount(7)
        self.tree.setHeaderLabels(["Filename / Folder", "Sel", "Status", "Progress", "Speed", "ETA", "Size"])

        self.tree.setItemDelegate(ProgressBarDelegate(self.tree))
        
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.tree.setColumnWidth(1, 38)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.tree.header().setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        self.tree.header().setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        self.tree.header().setSectionResizeMode(5, QHeaderView.ResizeMode.Interactive)
        self.tree.header().setSectionResizeMode(6, QHeaderView.ResizeMode.Interactive)
        
        saved_widths = self.settings.get("column_widths", {})
        if saved_widths:
            for i in range(self.tree.columnCount()):
                width = saved_widths.get(str(i))
                if width:
                    self.tree.setColumnWidth(i, width)
        else:
            self.tree.setColumnWidth(0, 320)
            self.tree.setColumnWidth(2, 110)
            self.tree.setColumnWidth(3, 110)
            self.tree.setColumnWidth(4, 90)
            self.tree.setColumnWidth(5, 80)
            self.tree.setColumnWidth(6, 120)

        self.tree.header().moveSection(1, 0)
        
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.show_tree_context_menu)
        
        self.tree.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.tree.installEventFilter(self)
        
        self.tree.itemClicked.connect(self.handle_item_clicked)
        self.tree.itemSelectionChanged.connect(self.handle_item_selection_changed)
        main_layout.addWidget(self.tree, stretch=1)

        # 3. ACTION CONTROLS BAR
        action_layout = QHBoxLayout()
        action_layout.setSpacing(8)
        
        self.select_all_btn = QPushButton("Select All")
        self.select_all_btn.clicked.connect(self.toggle_select_all)
        action_layout.addWidget(self.select_all_btn)
        
        self.start_btn = QPushButton("Start / Resume")
        self.start_btn.setObjectName("successBtn")
        self.start_btn.clicked.connect(self.start_downloads)
        action_layout.addWidget(self.start_btn)
        
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setObjectName("warningBtn")
        self.pause_btn.clicked.connect(self.pause_selected)
        action_layout.addWidget(self.pause_btn)
        
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("dangerBtn")
        self.cancel_btn.clicked.connect(self.cancel_selected)
        action_layout.addWidget(self.cancel_btn)
        
        self.retry_btn = QPushButton("Retry")
        self.retry_btn.setObjectName("purpleBtn")
        self.retry_btn.clicked.connect(self.retry_selected)
        action_layout.addWidget(self.retry_btn)

        self.force_redownload_btn = QPushButton("Force Redownload")
        self.force_redownload_btn.setObjectName("darkRedBtn")
        self.force_redownload_btn.clicked.connect(self.force_redownload_selected)
        action_layout.addWidget(self.force_redownload_btn)

        self.copy_log_btn = QPushButton("Copy Error Details")
        self.copy_log_btn.clicked.connect(self.copy_selected_error_log)
        action_layout.addWidget(self.copy_log_btn)
        
        self.delete_btn = QPushButton("Delete")
        self.delete_btn.clicked.connect(self.delete_selected)
        action_layout.addWidget(self.delete_btn)
        
        action_layout.addStretch()
        
        self.extract_checkbox = QCheckBox("Auto-Extract")
        self.extract_checkbox.setChecked(self.settings.get("extract_after_download", False))
        # Flips auto-extract for this session; the saved default changes in Settings.
        self.extract_checkbox.toggled.connect(lambda on: setattr(self.engine, "auto_extract", on))
        action_layout.addWidget(self.extract_checkbox)
        
        clear_btn = QPushButton("Clear Done")
        clear_btn.clicked.connect(self.clear_finished)
        action_layout.addWidget(clear_btn)
        
        main_layout.addLayout(action_layout)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_selected()
        elif event.key() == Qt.Key.Key_F:
            self.force_redownload_selected()
        else:
            super().keyPressEvent(event)

    def eventFilter(self, source, event):
        if source == self.text_links and event.type() == QEvent.Type.KeyPress:
            if event.matches(QKeySequence.StandardKey.Paste):
                self.paste_from_clipboard()
                return True
        if hasattr(self, 'tree') and source == self.tree and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
                self.delete_selected()
                return True
            if event.key() == Qt.Key.Key_F:
                self.force_redownload_selected()
                return True
            if event.key() == Qt.Key.Key_S:
                self.start_downloads()
                return True
            if event.key() == Qt.Key.Key_P:
                self.pause_selected()
                return True
            if event.key() == Qt.Key.Key_Space:
                selected = self.get_selected_tasks()
                if selected:
                    if selected[0].status in ("Downloading", "Starting..."):
                        self.pause_selected()
                    else:
                        self.start_downloads()
                return True
            if event.key() == Qt.Key.Key_C:
                self.cancel_selected()
                return True
            if event.key() == Qt.Key.Key_R:
                self.retry_selected()
                return True
        return super().eventFilter(source, event)

    def switch_theme(self, theme_name):
        self.settings["theme"] = theme_name
        save_settings(self.settings)
        apply_theme(QApplication.instance(), theme_name)
        if hasattr(self, 'theme_dark_action') and hasattr(self, 'theme_light_action'):
            self.theme_dark_action.setChecked(theme_name == "Dark")
            self.theme_light_action.setChecked(theme_name == "Light")

    def show_tree_context_menu(self, position):
        item = self.tree.itemAt(position)
        if item and not any(t.tree_item and t.tree_item.checkState(1) == Qt.CheckState.Checked for t in self.tasks):
            if not item.isSelected():
                self.tree.clearSelection()
            self.tree.setCurrentItem(item)
            item.setSelected(True)

        menu = QMenu(self)
        menu.addAction("[S] Start / Resume", self.start_downloads)
        menu.addAction("[P] Pause", self.pause_selected)
        menu.addAction("[C] Cancel", self.cancel_selected)
        menu.addSeparator()
        menu.addAction("Extract Now", self.manual_extract_selected)
        menu.addAction("Open Folder", self.open_selected_folder)
        if any(t.is_update and t.status == "Completed" for t in self.get_selected_tasks()):
            menu.addAction("Install Update", self.install_selected_update)
        menu.addSeparator()
        menu.addAction("[R] Retry", self.retry_selected)
        menu.addAction("[F] Force Redownload", self.force_redownload_selected)
        menu.addAction("Copy Error Details", self.copy_selected_error_log)
        menu.addSeparator()
        menu.addAction("Schedule download at specific interval", self.schedule_selected_downloads)
        menu.addSeparator()
        menu.addAction("Delete", self.delete_selected)
        menu.exec(self.tree.viewport().mapToGlobal(position))

    def get_or_create_batch_item(self, folder_name):
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item.text(0) == folder_name:
                return item

        batch_item = QTreeWidgetItem(self.tree)
        batch_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        batch_item.setText(0, folder_name)
        batch_item.setCheckState(1, Qt.CheckState.Unchecked)
        batch_item.setExpanded(True)
        return batch_item

    def open_selected_folder(self):
        tasks = self.get_selected_tasks()
        if not tasks:
            return
            
        # Open the folder of the first selected task
        folder_path = tasks[0].save_dir
        if not os.path.exists(folder_path):
            QMessageBox.information(self, "Folder Not Found", f"The folder does not exist yet:\n{folder_path}")
            return
            
        try:
            if sys.platform == 'win32':
                os.startfile(folder_path)
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', folder_path])
            else:
                subprocess.Popen(['xdg-open', folder_path])
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not open folder:\n{e}")

    def trigger_history_save(self):
        self.engine.trigger_history_save()

    def add_task_to_ui(self, task):
        batch_item = self.get_or_create_batch_item(task.folder_name)
        
        child_item = QTreeWidgetItem(batch_item)
        child_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        
        child_item.setText(0, task.filename)

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
            self.trigger_history_save()

    def copy_selected_error_log(self):
        for task in self.get_selected_tasks():
            if "Error" in task.status:
                self.copy_error_log(task)
                return
        QMessageBox.information(self, "No Error Selected", "Select a failed task first, then copy its error details.")

    def copy_error_log(self, task):
        # Read the newest backup too, so a rollover right after the failure
        # doesn't hide its lines. Oldest first, matching the log's own order.
        log_paths = [p for p in (LOG_PATH + ".1", LOG_PATH) if os.path.exists(p)]
        if not log_paths:
            QMessageBox.information(self, "No Log", "No error log found.")
            return
            
        try:
            logs = []
            for log_path in log_paths:
                with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                    logs.extend(f.readlines())
                
            keywords = [task.link, task.file_id, task.filename]
            # Filter out INFO level logs, keep ERROR, WARNING, CRITICAL or exception tracebacks
            error_logs = [line for line in logs if " - INFO - " not in line]
            matching_logs = [line for line in error_logs if any(keyword and keyword in line for keyword in keywords)]
            relevant_logs = "".join(matching_logs[-20:] if matching_logs else error_logs[-20:])
            
            if not relevant_logs.strip():
                if task.error_message:
                    relevant_logs = f"Error: {task.error_message}\n"
                else:
                    QMessageBox.information(self, "Log Empty", "No error log details found for this task.")
                    return
                
            clipboard = QApplication.clipboard()
            log_label = "Matching error logs" if matching_logs else ("Error details" if task.error_message and not matching_logs and not error_logs else "Recent error logs")
            detail_text = f"Task File: {task.filename}\nTask Link: {task.link}\nStatus: {task.status}"
            if task.error_message:
                detail_text += f"\nError Detail: {task.error_message}"
            clipboard.setText(f"{detail_text}\n\n{log_label}:\n{relevant_logs}")
            QMessageBox.information(self, "Log Copied", "Relevant error logs have been copied to your clipboard.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not read log file: {e}")

    def load_tasks_from_history(self):
        # The engine already loaded the history; this only builds the rows.
        for task in list(self.tasks):
            self.add_task_to_ui(task)

        # Collapse batches that are already completed on app startup
        for i in range(self.tree.topLevelItemCount()):
            batch_item = self.tree.topLevelItem(i)
            folder_tasks = [t for t in self.tasks if t.folder_name == batch_item.text(0)]
            if folder_tasks and all(t.status in ("Completed", "Extracted", "Extract Cancelled") for t in folder_tasks):
                batch_item.setExpanded(False)
            else:
                batch_item.setExpanded(True)

    def import_links_from_file(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Import Links", "", "Text Files (*.txt);;All Files (*)")
        if file_path:
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                    current_text = self.text_links.toPlainText()
                    if current_text.strip():
                        self.text_links.setText(current_text + "\n" + content)
                    else:
                        self.text_links.setText(content)
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to read file:\n{e}")

    def open_github_link(self):
        QDesktopServices.openUrl(QUrl("https://github.com/billysams21/SilverSpoon"))
        
    def open_contact_link(self):
        QDesktopServices.openUrl(QUrl("https://github.com/billysams21/SilverSpoon/issues"))
        
    def open_donate_link(self):
        QDesktopServices.openUrl(QUrl("https://ko-fi.com/billysm23"))

    def show_contributing_dialog(self):
        QMessageBox.information(self, "Contributing Guide",
            "<h3>Contributing to SilverSpoon</h3>"
            "<p>We welcome contributions! Please see the <b>CONTRIBUTING.md</b> file in the repository for full details.</p>"
            "<p><b>Quick Rules:</b></p>"
            "<ul>"
            "<li>Always work on the <code>dev</code> branch.</li>"
            "<li>Carefully test your changes before submitting a PR.</li>"
            "<li>Report bugs via the GitHub Issues tab.</li>"
            "</ul>"
        )

    def show_about_dialog(self):
        QMessageBox.about(self, "About SilverSpoon",
            f"<h3>SilverSpoon {CURRENT_VERSION}</h3>"
            "<p>A simple, fast bulk downloader for FuckingFast and DataNodes links developed by billysams21.</p>"
            "<p>Select your links, paste them in, and hit Add!</p>"
            "<p>Licensed under the GNU GPLv3.</p>"
            "<hr>"
            "<h4>Changelog (v1.6.0 - Short):</h4>"
            "<ul>"
            "<li><b>New:</b> Multi-connection downloads for direct links (Settings → Connections per Download).</li>"
            "<li><b>New:</b> FileKeeper (<code>filekeeper.net</code>) and BZZHR (<code>bzzhr.to</code>) links are supported.</li>"
            "<li><b>New:</b> Extraction progress bar, cancellable extraction, and a confirmation before re-extracting.</li>"
            "<li><b>New:</b> Auto-extract skips archives that are already extracted.</li>"
            "<li><b>New:</b> Any direct download link can be queued, not just FuckingFast/DataNodes.</li>"
            "<li><b>New:</b> Updates download through the normal queue; install now or on next open.</li>"
            "<li><b>New:</b> Only one copy of SilverSpoon runs at a time.</li>"
            "<li><b>Fix:</b> DataNodes links work again with DataNodes' new two-step download page.</li>"
            "<li><b>Fix:</b> A failed resolution no longer keeps re-spawning the browser.</li>"
            "</ul>"
            "<hr>"
            "<h4>Changelog (v1.5.1 - Short):</h4>"
            "<ul>"
            "<li><b>New:</b> Added support for DataNodes (<code>datanodes.to</code>) links with auto Turnstile & countdown bypass.</li>"
            "<li><b>New:</b> Smart URL and filename unquoting for clean batch folders and task names.</li>"
            "<li><b>Fix:</b> Optimized executable build footprint, saving ~187 MB uncompressed.</li>"
            "<li><b>Fix:</b> Build script safety to prevent binary lock errors during packaging.</li>"
            "</ul>"
            "<hr>"
            "<p><i>See CHANGELOG.md for full details.</i></p>"
        )

    def show_warning_dialog(self):
        dialog = WarningDialog(self.settings, self)
        dialog.exec()
        save_settings(self.settings)

    def show_warning_dialog_manual(self):
        dialog = WarningDialog(self.settings, self)
        dialog.dont_show_checkbox.setChecked(not self.settings.get("show_warning_dialog", True))
        dialog.exec()
        save_settings(self.settings)

    def manual_update_check(self):
        self.manual_checker = UpdateCheckerThread(CURRENT_VERSION, GITHUB_REPO, get_settings_path(), force=True)
        self.manual_checker.update_available.connect(self.prompt_update)
        self.manual_checker.check_finished.connect(self.update_last_check_time)
        self.manual_checker.no_update_found.connect(lambda: QMessageBox.information(self, "Up to date", "You are already using the latest version of SilverSpoon!"))
        self.manual_checker.error_checking.connect(lambda err: QMessageBox.warning(self, "Update Check Failed", f"Could not check for updates:\n{err}"))
        self.manual_checker.start()
        
    def update_last_check_time(self, timestamp):
        self.settings["last_update_check"] = timestamp
        save_settings(self.settings)
        self.settings = load_settings()
        
    def prompt_update(self, version, changelog, download_url):
        current_exe_dir = os.path.dirname(sys.executable)
        test_file = os.path.join(current_exe_dir, ".update_test_permission")
        try:
            with open(test_file, 'w') as f:
                f.write("test")
            os.remove(test_file)
        except PermissionError:
            QMessageBox.warning(
                self, "Update Available (Admin Required)",
                f"Version {version} is available!\n\n"
                f"However, SilverSpoon is located in a protected folder:\n{current_exe_dir}\n\n"
                "Please run SilverSpoon as Administrator to update automatically, or move it to a normal folder like Downloads or Desktop."
            )
            return
        except Exception:
            pass
            
        # Don't queue the same version twice — but re-offer if the previous try
        # failed/was cancelled, or its downloaded file is gone.
        for t in self.tasks:
            if (getattr(t, "is_update", False) and t.update_version == version
                    and t.status not in ("Error", "Cancelled")
                    and os.path.exists(t.filepath)):
                return

        # Add the update to the normal download queue so it downloads through the
        # proven engine (pause/resume/Range-resume) instead of a fragile modal
        # downloader. The user starts it like any other task; on completion it
        # offers to install now or on next launch.
        task = self._make_update_task(version, download_url)
        self.add_task_to_ui(task)

        dialog = QDialog(self)
        dialog.setWindowTitle(f"Update Available: {version}")
        dialog.setMinimumWidth(500)

        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(f"<b>A new version ({version}) is available!</b>"))
        layout.addWidget(QLabel(
            f'Added to your download queue as "<b>{task.folder_name}</b>". '
            "Start it now or later — you can pause and resume it like any "
            "other download."))

        text_edit = QTextEdit()
        text_edit.setReadOnly(True)
        text_edit.setMarkdown(changelog)
        layout.addWidget(text_edit)

        btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No)
        btn_box.button(QDialogButtonBox.StandardButton.Yes).setText("Start download now")
        btn_box.button(QDialogButtonBox.StandardButton.No).setText("I'll start it later")
        btn_box.accepted.connect(dialog.accept)
        btn_box.rejected.connect(dialog.reject)
        layout.addWidget(btn_box)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            task.status = "Pending"
            task.error_message = ""
            task.cancel_flag = False
            task.pause_flag = False
            
    def _apply_downloaded_update(self, zip_path, version=None):
        # Apply an already-downloaded release zip: extract it, then hand off to a
        # batch script that swaps the app directory and restarts (same robust
        # move -> robocopy -> marker-verify -> rollback flow as before).
        self.settings.pop("pending_update", None)
        save_settings(self.settings)
        if zip_path and os.path.exists(zip_path):
            extract_dir = os.path.join(tempfile.gettempdir(), f"silverspoon_extract_{int(time.time())}")
            
            try:
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(extract_dir)
                    
                new_app_dir = None
                for root, _, files in os.walk(extract_dir):
                    if any(file.lower() == "silverspoon.exe" for file in files):
                        new_app_dir = root
                        break

                if not new_app_dir:
                    raise Exception("Could not find SilverSpoon.exe inside the downloaded zip.")

                internal_dir = os.path.join(new_app_dir, "_internal")
                has_curl_metadata = os.path.isdir(internal_dir) and any(
                    entry.startswith("curl_cffi-")
                    and entry.endswith(".dist-info")
                    and os.path.isfile(os.path.join(internal_dir, entry, "METADATA"))
                    for entry in os.listdir(internal_dir)
                )
                if not has_curl_metadata:
                    raise Exception(
                        "The downloaded release is incomplete: curl_cffi metadata is missing. "
                        "Please download the complete SilverSpoon folder ZIP."
                    )
                    
                current_exe = sys.executable
                current_exe_name = os.path.basename(current_exe)
                current_app_dir = os.path.dirname(current_exe)
                
                if not current_exe_name.lower().startswith("silverspoon"):
                    msg_box = QMessageBox(self)
                    msg_box.setWindowTitle("Update Downloaded (Manual Action Required)")
                    msg_box.setText(
                        f"The update has been downloaded and extracted to:\n{extract_dir}\n\n"
                        "Because you are running SilverSpoon from a differently named executable or script, "
                        "the automatic replacement was aborted to keep you safe."
                    )
                    
                    copy_btn = msg_box.addButton("Copy Directory Path", QMessageBox.ButtonRole.ActionRole)
                    ok_btn = msg_box.addButton(QMessageBox.StandardButton.Ok)
                    msg_box.setDefaultButton(ok_btn)
                    
                    msg_box.exec()
                    
                    if msg_box.clickedButton() == copy_btn:
                        QApplication.clipboard().setText(extract_dir)
                        QMessageBox.information(self, "Copied", "Directory path copied to clipboard.")
                        
                    return
                
                self.engine.save_now()
                save_settings(self.settings)

                bat_path = os.path.join(tempfile.gettempdir(), f"silverspoon_update_{int(time.time())}.bat")
                backup_app_dir = current_app_dir + ".previous"
                success_marker = os.path.join(tempfile.gettempdir(), f"silverspoon_update_success_{int(time.time())}.marker")
                with open(bat_path, 'w') as bat:
                    bat.write('@echo off\n')
                    bat.write('echo Updating SilverSpoon...\n')
                    bat.write('set PYINSTALLER_RESET_ENVIRONMENT=1\n')
                    bat.write('set _MEIPASS=\n')
                    bat.write('set _MEIPASS2=\n')
                    bat.write(f'del /f /q "{success_marker}" > nul 2>&1\n')
                    bat.write(f'if exist "{backup_app_dir}" rmdir /s /q "{backup_app_dir}"\n')
                    bat.write(':wait_for_exit\n')
                    bat.write(f'move "{current_app_dir}" "{backup_app_dir}" > nul 2>&1\n')
                    bat.write('if not errorlevel 1 goto install\n')
                    bat.write('timeout /t 1 /nobreak > nul\n')
                    bat.write('goto wait_for_exit\n')
                    bat.write(':install\n')
                    bat.write(f'robocopy "{new_app_dir}" "{current_app_dir}" /E /COPY:DAT /R:3 /W:1 > nul\n')
                    bat.write('if errorlevel 8 goto rollback\n')
                    bat.write(f'set "SILVERSPOON_UPDATE_SUCCESS_MARKER={success_marker}"\n')
                    bat.write(f'start "" "{current_exe}"\n')
                    bat.write('for /l %%i in (1,1,30) do (\n')
                    bat.write(f'    if exist "{success_marker}" goto success\n')
                    bat.write('    timeout /t 1 /nobreak > nul\n')
                    bat.write(')\n')
                    bat.write(':rollback\n')
                    bat.write(f'if exist "{current_app_dir}" rmdir /s /q "{current_app_dir}"\n')
                    bat.write(f'move "{backup_app_dir}" "{current_app_dir}" > nul 2>&1\n')
                    bat.write(f'if exist "{current_exe}" start "" "{current_exe}"\n')
                    bat.write('goto cleanup\n')
                    bat.write(':success\n')
                    bat.write(f'rmdir /s /q "{backup_app_dir}" > nul 2>&1\n')
                    bat.write(':cleanup\n')
                    bat.write(f'del /f /q "{success_marker}" > nul 2>&1\n')
                    bat.write(f'rmdir /s /q "{extract_dir}" > nul 2>&1\n')
                    bat.write(f'del /q "{zip_path}" > nul 2>&1\n')
                    bat.write('del "%~f0"\n')
                
                CREATE_NO_WINDOW = 0x08000000
                subprocess.Popen(
                    ["cmd.exe", "/c", bat_path],
                    creationflags=CREATE_NO_WINDOW,
                    close_fds=True,
                    # The updater must not retain the app directory as its working directory,
                    # or Windows will prevent the batch file from renaming that directory.
                    cwd=tempfile.gettempdir(),
                )
                
                QApplication.quit()
                sys.exit(0)
                
            except Exception as e:
                QMessageBox.critical(self, "Update Failed", f"Failed to apply the update:\n{str(e)}")
        else:
            QMessageBox.warning(
                self, "Update",
                "The downloaded update file is missing. Please download it again.")

    def _make_update_task(self, version, download_url):
        # Download to the visible Save-To directory, like every other download,
        # so the user can find it (and it persists for an install-on-next-open).
        base_dir = os.path.abspath(self.dir_input.text())
        task = DownloadTask(download_url, base_dir, f"SilverSpoon Update {version}")
        task.is_update = True
        task.update_version = version
        task.is_selected = True
        return task

    def _check_update_task_done(self):
        """When a queued update download finishes, offer to install it. Skips a
        version already deferred to next-open so it isn't prompted twice."""
        pending_v = (self.settings.get("pending_update") or {}).get("version")
        for t in self.tasks:
            if (getattr(t, "is_update", False) and t.status == "Completed"
                    and not t.install_handled
                    and t.update_version != pending_v
                    and os.path.exists(t.filepath)):
                t.install_handled = True
                # Save now, not debounced: "Install now" quits the app.
                self.engine.save_now()
                self._prompt_install(t)

    def install_selected_update(self):
        """Context menu: install a downloaded update the user put off earlier."""
        for t in self.get_selected_tasks():
            if t.is_update and t.status == "Completed" and os.path.exists(t.filepath):
                self._prompt_install(t)
                return
        QMessageBox.information(
            self, "No Update Selected",
            "Select a completed SilverSpoon update download first.")

    def _prompt_install(self, task):
        box = QMessageBox(self)
        box.setWindowTitle("Update Downloaded")
        box.setText(
            f"SilverSpoon {task.update_version} has finished downloading.\n\n"
            "Install it now (the app will restart), or on the next launch?")
        now_btn = box.addButton("Install now", QMessageBox.ButtonRole.AcceptRole)
        later_btn = box.addButton("Install on next open", QMessageBox.ButtonRole.ActionRole)
        box.addButton("Not yet", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(now_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked == now_btn:
            self._apply_downloaded_update(task.filepath, task.update_version)
        elif clicked == later_btn:
            self.settings["pending_update"] = {
                "zip": task.filepath, "version": task.update_version}
            save_settings(self.settings)
            QMessageBox.information(
                self, "Update Scheduled",
                f"SilverSpoon {task.update_version} will be installed the next "
                "time you open the app.")

    def _check_pending_update(self):
        """On startup, offer to install an update that was deferred earlier."""
        pending = self.settings.get("pending_update")
        if not pending:
            return
        zip_path, version = pending.get("zip"), pending.get("version")
        if not zip_path or not os.path.exists(zip_path):
            self.settings.pop("pending_update", None)
            save_settings(self.settings)
            return
        reply = QMessageBox.question(
            self, "Install Update",
            f"SilverSpoon {version} was downloaded earlier and is ready to "
            "install.\n\nInstall it now? The app will restart.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self._apply_downloaded_update(zip_path, version)
        # If No, keep pending_update so it asks again next open.

    def open_settings_dialog(self):
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec():
            # Merge (not replace) so keys the dialog doesn't manage — e.g. the
            # off-peak "schedule" — are preserved.
            self.engine.update_settings(dialog.get_updated_settings())
            self.switch_theme(self.settings.get("theme", "Dark"))
            default_dir = self.settings.get("default_save_dir", os.path.join(os.path.expanduser("~"), "Downloads"))
            self.dir_input.setText(default_dir)
            self.extract_checkbox.setChecked(self.settings.get("extract_after_download", False))

    # ------------------------------------------------------------------
    # Download scheduling
    # ------------------------------------------------------------------
    def open_queue_scheduler(self):
        """File menu: schedule the whole queue."""
        self._open_scheduler(targets=None, scope_label="entire queue")

    def schedule_selected_downloads(self):
        """Right-click: schedule only the selected download(s)."""
        tasks = self.get_selected_tasks()
        if not tasks:
            QMessageBox.information(self, "No Selection",
                                    "Select one or more downloads to schedule.")
            return
        uids = [t.uid for t in tasks]
        label = (f"{len(tasks)} selected download(s)" if len(tasks) != 1
                 else os.path.basename(tasks[0].filename))
        self._open_scheduler(targets=uids, scope_label=label)

    def _open_scheduler(self, targets, scope_label):
        dialog = DownloadSchedulerDialog(self.schedule, scope_label, self)
        if not dialog.exec():
            return
        # None => whole queue; a list of task uids => only those downloads.
        self.engine.set_schedule(dialog.get_schedule(), targets)

        # (Un)register the Windows wake timer to match the saved settings.
        if self.schedule.get("enabled") and self.schedule.get("wake_timer"):
            executable, arguments = self._launch_command()
            ok, msg = offpeak.register_wake_task(self.schedule, executable, arguments)
            title = "Wake Timer" if ok else "Wake Timer Unavailable"
            (QMessageBox.information if ok else QMessageBox.warning)(self, title, msg)
        else:
            offpeak.unregister_wake_task()

        self._refresh_schedule_indicator()

    def _refresh_wake_task(self):
        """On startup, re-register an enabled wake task so it uses the current
        launch command (e.g. --background); older tasks were only updated when
        the schedule dialog was saved. Silent and off the UI thread (PowerShell
        is slow); Register-ScheduledTask -Force replaces the task in place."""
        if sys.platform != "win32":
            return
        if not (self.schedule.get("enabled") and self.schedule.get("wake_timer")):
            return
        sched = dict(self.schedule)
        executable, arguments = self._launch_command()

        def worker():
            ok, msg = offpeak.register_wake_task(sched, executable, arguments)
            if not ok:
                logging.error("Could not refresh the wake timer task: %s", msg)

        threading.Thread(target=worker, daemon=True).start()

    def _refresh_schedule_indicator(self):
        """Alt C: compact dot. Hidden when disabled; hover shows the full text."""
        text = offpeak.describe_schedule(self.schedule)
        try:
            window_active = bool(self.engine.offpeak_controller._active)
        except Exception:
            window_active = False
        start = self.schedule.get("start", "") if isinstance(self.schedule, dict) else ""
        self.schedule_dot.set_state(text, window_active=window_active, start_hhmm=start)

    def _launch_command(self):
        """Executable + argument string used by the wake task to relaunch the app."""
        if getattr(sys, "frozen", False):
            return sys.executable, "--background"
        return sys.executable, f'"{os.path.abspath(__file__)}" --background'

    def _process_engine_events(self):
        """Handle what the engine reported since the last tick (it runs the
        scheduler itself, on its own thread)."""
        for event in self.engine.events_since(self._last_event_id):
            self._last_event_id = event["id"]
            if event["type"] in ("schedule_edge", "schedule_changed"):
                self._refresh_schedule_indicator()
            elif event["type"] == "offpeak_summary":
                self._show_offpeak_summary(event["summary"], event["report_path"])

    def _show_offpeak_summary(self, summary, report_path):
        gb = summary["bytes_downloaded"] / (1024 ** 3)
        mins = summary["duration_seconds"] / 60
        report_line = (f"<i>Report appended to {report_path}</i>" if report_path
                       else "<i>Note: the report file could not be written.</i>")
        text = (
            f"<b>Scheduled download window finished.</b><br><br>"
            f"Files completed: <b>{summary['files_completed']}</b><br>"
            f"Downloaded: <b>{gb:.2f} GB</b><br>"
            f"Active duration: <b>{mins:.0f} min</b><br>"
            f"Average speed: <b>{summary['avg_speed_mbps']:.2f} MB/s</b><br>"
            f"Peak speed: <b>{summary['peak_speed_mbps']:.2f} MB/s</b><br><br>"
            f"{report_line}"
        )
        QMessageBox.information(self, "Download Scheduler — Summary", text)

    def browse_dir(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Save Directory", self.dir_input.text())
        if folder:
            self.dir_input.setText(os.path.abspath(folder))

    def paste_from_clipboard(self):
        clipboard = QApplication.clipboard()
        mime_data = clipboard.mimeData()
        
        text = ""
        if mime_data.hasHtml():
            # If the clipboard contains HTML, extract href links
            html = mime_data.html()
            # Simple regex to find hrefs
            import re
            links = re.findall(r'href=[\'"]?([^\'" >]+)', html)
            if links:
                # Filter out anything that clearly isn't an http link
                text = "\n".join(link for link in links if link.startswith('http'))
        
        # Fallback to plain text if no links were found in HTML or if it's just plain text
        if not text and mime_data.hasText():
            text = mime_data.text()

        if text:
            current_text = self.text_links.toPlainText()
            if current_text.strip():
                self.text_links.setText(current_text + "\n" + text)
            else:
                self.text_links.setText(text)

    def add_links(self):
        links = parse_links(self.text_links.toPlainText())
        if not links:
            return

        save_dir = os.path.abspath(self.dir_input.text())

        folder_name, ok = QInputDialog.getText(
            self, 
            "Batch Folder Name", 
            "Enter a folder name for these files:\n(This groups main game and optional files together)",
            QLineEdit.EchoMode.Normal,
            suggest_folder(links)
        )
        
        if not ok or not folder_name.strip():
            return
            
        for task in self.engine.add_links(links, save_dir, folder_name.strip()):
            self.add_task_to_ui(task)
            
        self.text_links.clear()

    def toggle_select_all(self):
        all_checked = True
        total_items = 0

        for i in range(self.tree.topLevelItemCount()):
            batch_item = self.tree.topLevelItem(i)
            if batch_item.checkState(1) != Qt.CheckState.Checked:
                all_checked = False
            for j in range(batch_item.childCount()):
                total_items += 1
                if batch_item.child(j).checkState(1) != Qt.CheckState.Checked:
                    all_checked = False
                    
        if total_items == 0:
            return
            
        self.is_all_selected = not all_checked
        state = Qt.CheckState.Checked if self.is_all_selected else Qt.CheckState.Unchecked

        for i in range(self.tree.topLevelItemCount()):
            batch_item = self.tree.topLevelItem(i)
            batch_item.setCheckState(1, state)
            for j in range(batch_item.childCount()):
                child_item = batch_item.child(j)
                child_item.setCheckState(1, state)
                
        for task in self.tasks:
            task.is_selected = self.is_all_selected

    def handle_item_clicked(self, item, col):
        if col == 1:
            state = item.checkState(1)

            if item.parent() is None:
                for i in range(item.childCount()):
                    child = item.child(i)
                    child.setCheckState(1, state)
                    task = next((t for t in self.tasks if t.tree_item == child), None)
                    if task:
                        task.is_selected = (state == Qt.CheckState.Checked)
            else:
                task = next((t for t in self.tasks if t.tree_item == item), None)
                if task:
                    task.is_selected = (state == Qt.CheckState.Checked)
                    
    def handle_item_selection_changed(self):
        for i in range(self.tree.topLevelItemCount()):
            top_item = self.tree.topLevelItem(i)
            if top_item.isSelected():
                top_item.setCheckState(1, Qt.CheckState.Checked)
            else:
                top_item.setCheckState(1, Qt.CheckState.Unchecked)

            for j in range(top_item.childCount()):
                child = top_item.child(j)
                if top_item.isSelected() or child.isSelected():
                    child.setCheckState(1, Qt.CheckState.Checked)
                    task = next((t for t in self.tasks if t.tree_item == child), None)
                    if task:
                        task.is_selected = True
                else:
                    child.setCheckState(1, Qt.CheckState.Unchecked)
                    task = next((t for t in self.tasks if t.tree_item == child), None)
                    if task:
                        task.is_selected = False

    def get_selected_tasks(self):
        checked = [t for t in self.tasks if t.tree_item and t.tree_item.checkState(1) == Qt.CheckState.Checked]
        if checked:
            return checked

        selected_items = self.tree.selectedItems()
        selected_tasks = []
        for item in selected_items:
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

    def _selected_uids(self):
        return [t.uid for t in self.get_selected_tasks()]

    def start_downloads(self):
        self.engine.start_tasks(self._selected_uids())

    def pause_selected(self):
        self.engine.pause_tasks(self._selected_uids())

    def cancel_selected(self):
        self.engine.cancel_tasks(self._selected_uids())

    def retry_selected(self):
        self.engine.retry_tasks(self._selected_uids())

    def _remove_tree_item(self, task):
        if not task.tree_item:
            return
        parent = task.tree_item.parent()
        if parent:
            parent.removeChild(task.tree_item)
            # If the batch is now empty, remove it too.
            if parent.childCount() == 0:
                idx = self.tree.indexOfTopLevelItem(parent)
                if idx >= 0:
                    self.tree.takeTopLevelItem(idx)

    def force_redownload_selected(self):
        tasks_to_redownload = self.get_selected_tasks()
        if not tasks_to_redownload:
            QMessageBox.information(self, "No Selection", "Select one or more tasks to force redownload.")
            return

        uids = [t.uid for t in tasks_to_redownload]
        files_with_progress = self.engine.redownload_with_progress(uids)
        if files_with_progress:
            reply = QMessageBox.warning(
                self, 'Confirm Force Redownload',
                f"You have selected {len(files_with_progress)} file(s) that already have download progress.\n\n"
                f"Forcing a redownload will PERMANENTLY DELETE the partially downloaded file(s) and start from 0%.\n\n"
                f"Are you sure you want to proceed?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        result = self.engine.force_redownload(uids)
        if result["skipped"] or result["failed"] or result["queued"] == 0:
            QMessageBox.information(
                self,
                "Force Redownload",
                f"Queued: {result['queued']}\nSkipped active tasks: {result['skipped']}\nFailed: {result['failed']}"
            )

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

        for task in tasks_to_delete:
            self._remove_tree_item(task)
        self.engine.delete_tasks([t.uid for t in tasks_to_delete], delete_files)

    def clear_finished(self):
        for task in self.engine.clear_finished():
            self._remove_tree_item(task)


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
            extracting = task.status == "Extracting..."
            if extracting:
                prog_str = f"{task.extract_progress:.1f}%" if task.extract_progress is not None else "-"
            else:
                prog_str = f"{task.progress:.1f}%" if task.status not in ("Extracted", "Extract Error") else "-"
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
            
            if task.tree_item.text(0) != task.filename:
                task.tree_item.setText(0, task.filename)
            task.tree_item.setText(2, task.status)
            # Apply word wrap to the tooltip text to avoid very long horizontal lines
            if "Error" in task.status and task.error_message:
                import textwrap
                wrapped_text = "\n".join(textwrap.wrap(task.error_message, width=60))
                task.tree_item.setToolTip(2, wrapped_text)
            elif task.status == "Waiting for you to solve CAPTCHA":
                task.tree_item.setToolTip(
                    2, "Complete the \"Verify you are human\" check in the browser window that opened.")
            else:
                task.tree_item.setToolTip(2, "")
            task.tree_item.setText(3, prog_str)
            task.tree_item.setText(4, speed_str)
            task.tree_item.setText(5, eta_str)
            task.tree_item.setText(6, size_str)
            
            if task.status == "Downloading":
                global_speed += task.speed
                
            # Store the progress and status in the item's data for the custom delegate to paint
            # (while extracting, the bar shows extraction progress instead).
            bar_value = (task.extract_progress or 0.0) if extracting else task.progress
            task.tree_item.setData(0, Qt.ItemDataRole.UserRole, bar_value)
            task.tree_item.setData(1, Qt.ItemDataRole.UserRole, task.status)
                
        # Update telemetry stat counters
        active_count = sum(1 for t in self.tasks if t.status in ("Downloading", "Starting...", "Solving CAPTCHA...", "Waiting for you to solve CAPTCHA", "Extracting..."))
        queued_count = sum(1 for t in self.tasks if t.status in ("Queued", "Pending"))
        done_count = sum(1 for t in self.tasks if t.status in ("Completed", "Extracted", "Extract Cancelled"))
        self.stat_active_val.setText(str(active_count))
        self.stat_queued_val.setText(str(queued_count))
        self.stat_done_val.setText(str(done_count))

        self.global_speed_label.setText(f"{global_speed:.2f} MB/s")
        if hasattr(self, 'speed_graph'):
            has_active_dl = any(t.status == "Downloading" for t in self.tasks) or global_speed > 0
            if has_active_dl:
                self.speed_graph.add_sample(global_speed)
            
        # Update top-level batch folders
        for i in range(self.tree.topLevelItemCount()):
            batch_item = self.tree.topLevelItem(i)
            total_dl = 0
            total_size = 0
            total_speed = 0.0
            
            all_completed = True
            any_error = False
            any_downloading = False
            extracting_progress = []

            child_count = batch_item.childCount()
            if child_count == 0:
                continue
                
            for j in range(child_count):
                child = batch_item.child(j)
                task = next((t for t in self.tasks if t.tree_item == child), None)
                if task:
                    # Add task.total_bytes if it's available
                    if hasattr(task, 'total_bytes') and task.total_bytes > 0:
                        total_dl += getattr(task, 'downloaded_bytes', 0)
                        total_size += task.total_bytes
                    elif task.status == "Downloading" and hasattr(task, 'total_bytes'):
                        total_dl += getattr(task, 'downloaded_bytes', 0)
                        total_size += getattr(task, 'total_bytes', 0)
                    else:
                        # Estimate total size for UI based on largest known file
                        ext = os.path.splitext(task.filename)[1].lower()
                        if ext in ('.rar', '.zip'):
                            largest_known = max([getattr(x, 'total_bytes', 0) for x in self.tasks if x.folder_name == batch_item.text(0)] + [0])
                            total_size += largest_known
                            
                    total_speed += getattr(task, 'speed', 0.0)
                    
                    if task.status not in ("Completed", "Extracted", "Extracting...", "Extract Cancelled"):
                        all_completed = False
                    if task.status == "Extracting...":
                        extracting_progress.append(task.extract_progress)
                    if "Error" in task.status:
                        any_error = True
                    if task.status in ("Downloading", "Starting...", "Pending"):
                        any_downloading = True
                        
            # Determine batch status
            batch_status = "Queued"
            if all_completed:
                batch_status = "Extracting..." if extracting_progress else "Completed"
            elif any_error:
                batch_status = "Contains Errors"
            elif any_downloading:
                batch_status = "Active"
                
            prog = (total_dl / total_size * 100) if total_size > 0 else 0
            prog_str = f"{prog:.1f}%"
            if batch_status == "Extracting...":
                # The whole folder extracts together, so its tasks share one value.
                known = [p for p in extracting_progress if p is not None]
                prog = sum(known) / len(known) if known else 0.0
                prog_str = f"{prog:.1f}%" if known else "-"
            speed_str = f"{total_speed:.2f} MB/s" if total_speed > 0 else "-"
            size_mb = total_size / (1024*1024)
            dl_mb = total_dl / (1024*1024)
            size_str = f"{dl_mb:.1f} / {size_mb:.1f} MB" if total_size > 0 else "-"
            
            eta_str = "-"
            if any_downloading and total_speed > 0 and total_size > 0:
                largest_known_size = 0
                for k in range(batch_item.childCount()):
                    t = next((x for x in self.tasks if x.tree_item == batch_item.child(k)), None)
                    if t and hasattr(t, 'total_bytes') and t.total_bytes > largest_known_size:
                        largest_known_size = t.total_bytes
                
                # Calculate ETA for the entire batch using all tasks
                remaining_bytes_batch = 0
                for k in range(batch_item.childCount()):
                    t = next((x for x in self.tasks if x.tree_item == batch_item.child(k)), None)
                    if t:
                        if hasattr(t, 'total_bytes') and t.total_bytes > 0:
                            remaining_bytes_batch += (t.total_bytes - getattr(t, 'downloaded_bytes', 0))
                        else:
                            ext = os.path.splitext(t.filename)[1].lower()
                            if ext in ('.rar', '.zip'):
                                remaining_bytes_batch += largest_known_size
                        
                if remaining_bytes_batch > 0:
                    eta_seconds = remaining_bytes_batch / (total_speed * 1024 * 1024)
                    eta_str = self.format_eta(eta_seconds)
            
            batch_item.setText(2, batch_status)
            batch_item.setToolTip(2, "")
            batch_item.setText(3, prog_str)
            batch_item.setText(4, speed_str)
            batch_item.setText(5, eta_str)
            batch_item.setText(6, size_str)
            
            # Store the progress and status in the item's data for the custom delegate to paint
            batch_item.setData(0, Qt.ItemDataRole.UserRole, prog)
            batch_item.setData(1, Qt.ItemDataRole.UserRole, batch_status)

        self._check_update_task_done()

    def manual_extract_selected(self):
        selected_tasks = self.get_selected_tasks()
        if not selected_tasks:
            QMessageBox.information(self, "No Selection", "Select one or more tasks to extract.")
            return

        uids = [t.uid for t in selected_tasks]
        # Re-extracting overwrites what's on disk, so confirm it first.
        already = self.engine.extract_preview(uids)
        skip = ()
        if already:
            shown = "\n".join(f"  • {name}" for name in already[:10])
            if len(already) > 10:
                shown += f"\n  … and {len(already) - 10} more"
            reply = QMessageBox.question(
                self, "Already Extracted",
                f"These are already extracted:\n\n{shown}\n\n"
                "Extract them again? Existing files will be overwritten.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                skip = already

        # An explicit "Extract Now" always extracts, even over existing files.
        self.engine.extract(uids, skip)

if __name__ == "__main__":
    app = QApplication(sys.argv)

    # Single-instance guard: prevent two copies from corrupting shared state
    _background_launch = "--background" in sys.argv
    # Lives next to the settings/history files it protects, so it is per-user
    # (a shared /tmp would let one user's instance block another's).
    _lock_path = os.path.expanduser("~/.silverspoon.lock")
    # If something else created a directory at our lock path, use a fallback so
    # QLockFile's LockFailedError cannot be confused with a real running instance.
    if os.path.isdir(_lock_path):
        _lock_path = os.path.expanduser("~/.silverspoon2.lock")
    _lock = QLockFile(_lock_path)
    _lock.setStaleLockTime(30 * 1000)  # treat locks older than 30 s (after a crash) as stale
    if not _lock.tryLock(0):
        if _lock.error() == QLockFile.LockError.LockFailedError:
            # Another live instance owns the lock.
            if not _background_launch:
                from PyQt6.QtWidgets import QMessageBox
                msg = QMessageBox()
                msg.setWindowTitle("SilverSpoon")
                msg.setText("SilverSpoon is already running.\nPlease use the existing instance.")
                msg.setIcon(QMessageBox.Icon.Warning)
                msg.exec()
            sys.exit(1)
        # Permission / unknown error — can't create the lock file but no other instance
        # is confirmed running, so allow the app to start in degraded (unprotected) mode.

    # Load settings to apply initial theme
    initial_settings = load_settings()
    apply_theme(app, initial_settings.get("theme", "Dark"))
    
    # Determine base directory for assets
    if hasattr(sys, '_MEIPASS'):
        base_dir = sys._MEIPASS
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        
    # Show splash screen
    splash_pixmap = QPixmap(os.path.join(base_dir, "SilverSpoon.png"))
    
    # If the image is extremely large, scale it down for the splash screen
    if not splash_pixmap.isNull():
        if splash_pixmap.width() > 600 or splash_pixmap.height() > 400:
            splash_pixmap = splash_pixmap.scaled(600, 400, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            
    splash = QSplashScreen(splash_pixmap, Qt.WindowType.WindowStaysOnTopHint)
    splash.show()
    
    # Allow Qt events to process so the splash screen renders immediately
    app.processEvents()
    
    # Setup window and load things while splash is visible
    window = MainWindow()
    update_success_marker = os.environ.get("SILVERSPOON_UPDATE_SUCCESS_MARKER")
    if update_success_marker:
        try:
            with open(update_success_marker, "w", encoding="utf-8") as marker:
                marker.write("SilverSpoon started successfully.\n")
        except OSError:
            pass
    
    # After 1 second (1000 ms), close splash and show main window
    QTimer.singleShot(1000, splash.close)
    QTimer.singleShot(1000, window.show)
    
    sys.exit(app.exec())
