# FitGirlDownloader

> **Note:** Currently, this tool ONLY supports `fuckingfast.co` links. Support for other hosts may be added in the future.

A Python-based bulk downloader designed to bypass Cloudflare protections on file-hosting sites like *fuckingfast.co* (often used by FitGirl Repacks). It automates the process of extracting direct download links and supports concurrent downloading with pause and resume capabilities.

## Features

* **Cloudflare Bypass:** Uses `cloudscraper` to mimic a real browser and bypass anti-bot challenges.
* **Smart Folder Grouping:** Automatically groups multi-part files (e.g. `part01`, `part02`) into their own subfolder based on the common prefix.
* **Direct Link Extraction:** Automatically simulates the internal HTMX POST requests required to fetch the real `.rar` direct links.
* **Multi-threading:** Downloads multiple parts concurrently (default 3 workers) to maximize bandwidth.
* **Pause & Resume:** Safely pause your downloads or recover from network drops. The script checks existing file sizes and resumes using HTTP `Range` headers.
* **Graphical Interface:** Includes a clean, modern GUI built with PyQt6.
* **Command Line Interface:** Also includes a lightweight CLI script for server environments or automation.

## Requirements

* Python 3.10+
* Dependencies listed in `requirements.txt`

## Installation

1. Clone this repository:
   ```bash
   git clone https://github.com/billysams21/FitGirlDownloader.git
   cd FitGirlDownloader
   ```
2. Install the required Python packages (or do it inside virtual environment):
   ```bash
   pip install -r requirements.txt
   ```

## Usage

### Using the GUI (Recommended)
Launch the graphical interface:
```bash
python pyqt_downloader.py
```
1. Click **Browse...** to select your save directory.
2. Paste your `fuckingfast.co` links into the text box (one per line).
3. Click **Add Links to Queue**. The links will appear in the table below.
4. Select the checkboxes for the files you want to download.
5. (Optional) Check the **Extract after download** checkbox if you want files extracted automatically.
6. Click **Start Download** to begin downloading selected files (up to 3 concurrently).
7. Use the Pause All / Resume All buttons to manage your queue.

### Using the CLI
If you prefer the command line:
1. Put your links into `link.txt` (one per line).
2. Run the script:
   ```bash
   python downloader.py link.txt
   ```
*(Files will be downloaded to the current working directory).*

## Disclaimer

This tool is provided for educational and automation purposes only. The author is not responsible for the content downloaded using this tool. Please respect the terms of service of the file-hosting providers.
