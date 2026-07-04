@echo off
echo Building FitGirlDownloader executable...
echo This might take a minute or two.
echo.

:: We package the internal playwright browser binaries so it works on any computer
set PLAYWRIGHT_BROWSERS_PATH=0
C:\Python313\python.exe -m playwright install chromium

C:\Python313\Scripts\pyinstaller.exe --noconsole --onefile --add-binary "7z.exe;." --add-binary "7z.dll;." --hidden-import="playwright" --name "FitGirlDownloader" pyqt_downloader.py

echo.
echo Build complete! You can find the executable in the 'dist' folder.
pause
