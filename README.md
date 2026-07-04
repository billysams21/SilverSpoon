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
Launch the graphical interface (or double-click `FitGirlDownloader.exe`):
```bash
python pyqt_downloader.py
```

![App Screenshot 1](assets/screenshot1.png)

1. Click **Browse...** to select your base save directory.
2. Open the game link and click the provider you want to use (for now it's FuckingFast).
![FitGirl 1](assets/fitgirl1.png)
3. Copy the links you want to download.
![FitGirl 2](assets/fitgirl2.png)
4. Paste your `fuckingfast.co` links into the top text box (one per line).
![App Screenshot 2](assets/screenshot2.png)
5. Click **Add Links to Queue**. The links will appear in the table below, automatically grouped by folder name.
6. Click **Select All** (or check individual boxes) for the files you want to download.
7. (Optional) Check the **Extract after download** checkbox if you want files extracted automatically using the built-in 7-Zip engine.
8. Click the green **Start Selected** button to begin downloading (up to 3 concurrently).
![App Screenshot 3](assets/screenshot3.png)
9. Use the **Pause** and **Resume** buttons to manage your selected downloads at any time.

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
