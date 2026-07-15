# Changelog

All notable changes to this project will be documented in this file.

## [v1.2.1] - 2026-07-16

### New Features
* **Cross-Platform Extraction**: Added extraction support for Linux and macOS (`/usr/bin/7z` / `p7zip`) alongside Windows (`7-Zip`/`WinRAR`).
* **Context Menu & Keyboard Shortcuts**: Added a right-click context menu to the download table and handy keyboard shortcuts for starting (`S`), pausing (`P`), cancelling (`C`), retrying (`R`), redownloading (`F`), and deleting (`Delete`/`Backspace`) tasks.
* **Force Redownload**: Added a "Force Redownload" button/action to easily delete an existing downloaded file and restart the task from scratch.
* **Error Diagnostics & Logging**: Added descriptive hover tooltips (`HTTP status codes`, timeouts, disconnection reasons) on errored tasks, background error logging (`~/.silverspoon.log`), and a dedicated "Copy Error Details" button for easy troubleshooting.
* **License**: Added the `GNU General Public License v3.0` (`GPLv3`).

## [v1.2.0] - 2026-07-15

### New Features
* **Persistent Download History**: Automatically saves and restores your task queue, progress, and folder groupings across sessions.
* **Grouped Batch Folders**: Replaced the flat table with a collapsible tree view. Downloads are automatically grouped by batch, showing aggregated progress, speed, and ETA for the entire folder.
* **Live Speed & ETAs**: Added a real-time global download speed tracker and estimated time remaining (ETA) for individual files and entire batches.
* **Customizable UI**: Interactive, resizable columns that save their state so your layout is preserved across app restarts.
* **Paste from Clipboard**: Added a dedicated button to paste links safely as unstyled plain text.
* **Task & File Deletion**: Added a "Delete" button and keyboard shortcut support (`Delete`/`Backspace`) to remove tasks, complete with an option to permanently delete associated physical downloaded files.
* **Retry Action**: Added a dedicated "Retry Error" button to quickly restart failed downloads.

### Changes
* **Improved Selection Logic**: Added `Shift/Ctrl+Click` highlighting support and visually moved the selection column to the far left.
* **Extractor Thread Safety**: Fixed race conditions that could cause extraction threads to overlap when multiple batches finish or are loaded from history.
* **UI Polish**: Removed dotted focus boxes when clicking cells for a cleaner, modern look.


## [v1.1.0] - 2026-07-05

### New Features
* **Top Menu Bar**: Added a new top menu bar for easier navigation and quick access to tools.
* **Persistent Settings**: Added a Settings page (`File -> Settings`) with persistent configurations for your Base Save Directory, Max Concurrent Downloads, and Auto-extract preference.
* **Import Links**: You can now import links directly from `.txt` files via the File menu.
* **Batch Folder Prompt**: Automatically groups main game parts and optional files into the exact same folder when adding links, keeping your downloads perfectly organized.
* **Help Menu**: Added a Help menu containing quick links to the GitHub Repository, Contact Us (Issues), a Contributing Guide, and an About page.

### Changes
* **Action Buttons**: Consolidated 'Start' and 'Resume' into a single, smarter action button for a cleaner interface.