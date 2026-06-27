@echo off
setlocal
cd /D "%~dp0"

rem Find a Python launcher: prefer the py launcher, fall back to python on PATH.
set "PY=py"
where py >nul 2>nul || set "PY=python"

echo Installing build dependencies...
%PY% -m pip install --quiet flask pywebview pyinstaller || goto :err

echo Building app (this can take a couple of minutes)...
rem --onedir (a folder, not one big exe) so there is NO unpack step on launch -> starts
rem instantly. pythonnet + clr_loader must be fully collected or the EdgeChromium
rem (WebView2) backend silently falls back to the broken WinForms one in the frozen build.
%PY% -m PyInstaller --onedir --noconsole --clean --noconfirm ^
  --name DJCopyrightPrep ^
  --icon icon.ico ^
  --add-data "bin;bin" ^
  --add-data "redist;redist" ^
  --collect-all webview ^
  --collect-all pythonnet ^
  --collect-all clr_loader ^
  --copy-metadata pythonnet ^
  --exclude-module tkinter ^
  app.py || goto :err

echo.
echo DONE. Your app folder is at:  dist\DJCopyrightPrep\
echo Run dist\DJCopyrightPrep\DJCopyrightPrep.exe  (send the whole folder, zipped).
echo No Python needed on the target machine.
echo.
pause
exit /b 0

:err
echo.
echo BUILD FAILED. Make sure Python is installed and on PATH.
pause
exit /b 1
