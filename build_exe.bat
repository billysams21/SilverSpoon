@echo off
echo Building SilverSpoon distribution with bundled Playwright Chromium...
echo This can take several minutes and produces a large folder.
echo.

pyinstaller --noconfirm --clean --noconsole --onedir --icon="SilverSpoon.ico" --add-binary "7z.exe;." --add-binary "7z.dll;." --add-data "SilverSpoon.ico;." --add-data "SilverSpoon.png;." --add-data "theme_assets;theme_assets" --copy-metadata "curl_cffi" --collect-all "crx_unpack" --exclude-module "cv2" --exclude-module "numpy" --exclude-module "matplotlib" --exclude-module "scipy" --exclude-module "pandas" --exclude-module "torch" --exclude-module "torchvision" --exclude-module "IPython" --exclude-module "PIL" --exclude-module "Pillow" --exclude-module "tkinter" --exclude-module "unittest" --exclude-module "pytest" --exclude-module "PyQt6.QtPdf" --exclude-module "PyQt6.QtNetwork" --name "SilverSpoon" pyqt_downloader.py
if errorlevel 1 goto :error

powershell -NoProfile -Command "$source = Get-ChildItem (Join-Path $env:LOCALAPPDATA 'ms-playwright') -Directory -Filter 'chromium-*' | Where-Object { $_.Name -notlike 'chromium_headless*' } | Sort-Object Name -Descending | ForEach-Object { Join-Path $_.FullName 'chrome-win64' } | Where-Object { Test-Path (Join-Path $_ 'chrome.exe') } | Select-Object -First 1; if (-not $source) { Write-Error 'Playwright Chromium was not found under %LOCALAPPDATA%\ms-playwright.'; exit 1 }; $dest = 'dist\SilverSpoon\playwright-chromium'; if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }; New-Item -ItemType Directory -Path $dest -Force | Out-Null; Copy-Item -Path (Join-Path $source '*') -Destination $dest -Recurse -Force; Get-ChildItem -Path (Join-Path $dest 'locales') -Filter '*.pak' | Where-Object { $_.Name -notmatch '^(en-US|en-GB)\.pak$' } | Remove-Item -Force; @('setup.exe', 'elevated_tracing_service.exe', 'elevation_service.exe', 'notification_helper.exe', 'chrome_pwa_launcher.exe') | ForEach-Object { $p = Join-Path $dest $_; if (Test-Path $p) { Remove-Item $p -Force } }"
if errorlevel 1 goto :error

echo.
echo Build complete! Distribute the entire 'dist\SilverSpoon' folder.
pause
exit /b 0

:error
echo.
echo Build failed.
pause
exit /b 1
