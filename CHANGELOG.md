# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Fixes & Improvements
* **Updates download in the queue (no more crash):** The auto-updater no longer uses a separate modal downloader (which could crash mid-download on large releases). A new version is now added to your normal download queue — start it when you like, and pause/resume it like any other download. When it finishes, choose **Install now** (restart) or **Install on next open**; a deferred update is offered again the next time you launch. Update downloads skip the Cloudflare/CAPTCHA step and are never auto-extracted.

## [v1.5.0] - 2026-08-21

### New Features
* **Bento Grid UI & Dual Theme Engine**: Redesigned the application layout with a clean industrial Bento Card interface and introduced full Dark and Light theme support (switchable via `View -> Theme`), complete with bundled high-contrast checkbox and radio assets.
* **Live Speed Graph**: Added an interactive rolling download speed graph widget inside the Live Status card that renders real-time bandwidth metrics with smooth antialiased gradients.
* **Download Scheduler**: Added a `File -> Download Scheduler` dialog to define download windows (repeating weekly on chosen days, or a one-off date; windows may cross midnight) with clear 12-hour AM/PM time pickers. Individual tasks can also be scheduled via right-click **Schedule download at specific interval**. Includes a dedicated "Remove Schedule" button, date/day validation guards, Windows wake timers, and keep-awake power management.
* **Armed Schedule Indicator**: Added a compact, theme-aware mini clock indicator to the Live Status header that displays the active schedule details on hover and opens the scheduler dialog on click.

### Fixes & Improvements
* **Smart Clipboard Link Extractor**: Pasting from clipboard now automatically parses rich HTML data to extract valid `http` download links from formatted webpage tables or text selections.
* **Smart Startup Queue Folding**: Completed and extracted download batches are now collapsed by default when opening the application, keeping the active queue clean and organized.
* **Build & Packaging**: Updated the PyInstaller build configuration to bundle `theme_assets/` and added GitHub Actions CI workflows for automated headless test execution and code compilation.



## [v1.4.0] - 2026-08-03

### New Features
* **Bundled Playwright Chromium**: The application now ships with a bundled version of Playwright Chromium for the CAPTCHA solver to function.
* **Auto-Retry Failed Downloads**: Added an "Automatically retry failed downloads" option in Settings that will transparently requeue a failed or timed-out download up to 3 times before giving up.
* **Custom CAPTCHA Timeout**: Added a new setting to explicitly define how long the background browser should wait for a Cloudflare Turnstile token before timing out (defaults to 10 seconds).
* **Open Folder**: Added an "Open Folder" action to the right-click context menu to quickly launch the download directory in your system's file explorer.
* **Bandwidth Limiter**: Added a new setting to cap global download speed, letting you reserve network bandwidth for other applications.
* **CAPTCHA Solver Integration**: Replaced cloudscraper with a automated Chromium browser (but sometimes you need to check the box) to solve Cloudflare Turnstile challenges invisibly. 
* **Inline Progress Bars**: Download progress bars are now painted behind the filename and folder text directly within the tree view.
* **Manual Extraction**: Added an "Extract Now" option to the right-click context menu, allowing you to trigger archive extraction manually on selected batches.

### Fixes & Improvements
* **Resume Paused Stability**: Fixed a bug where a task could become stuck in a "Pausing..." state if the application was closed during a transition, and allowed "CAPTCHA Timeout" states to properly resume via the Start button.
* **Stable Speed & ETA**: Replaced per-chunk speed swings with a 3-second rolling average, yielding much more accurate ETA calculations for files and batches.
* **Smart Folder Names**: Improved the default folder naming logic when adding new links, automatically trimming out generic fitgirl prefixes.

## [v1.3.0] - 2026-07-17

### New Features
* **Auto-Updater**: Implemented a built-in automatic updater for Windows executables that seamlessly downloads, replaces the binary, and restarts the application.
* **VPN Warning Dialog**: Added a welcome dialog to warn users about aggressive Cloudflare blocking of known VPN IPs, which can cause persistent download failures.
* **Smart Default Save Directory**: The default save location now automatically detects and falls back to the user's "Downloads" folder on Windows (or the current directory otherwise).
* **Reset Settings**: Added a "Reset Defaults" button in the Settings menu to easily revert all configurations (including UI sizes and warning dialog visibility) to their factory defaults.
* **Spacebar Toggle**: You can now conveniently toggle pause and resume for selected downloads using the `Space` key.

### Fixes & Improvements
* **Directory Creation Stability**: Improved error handling when creating save directories during downloads, preventing crashes if the path is invalid or restricted.

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