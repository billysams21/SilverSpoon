@echo off
echo Building SilverSpoon executable...
echo This might take a minute or two.
echo.

C:\Python313\Scripts\pyinstaller.exe --noconsole --onefile --icon="SilverSpoon.ico" --add-binary "7z.exe;." --add-binary "7z.dll;." --add-data "SilverSpoon.ico;." --add-data "SilverSpoon.png;." --name "SilverSpoon" pyqt_downloader.py

echo.
echo Build complete! You can find the executable in the 'dist' folder.
pause
