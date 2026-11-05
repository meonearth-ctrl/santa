@echo off
rem Install Santa.bat - installs Santa for this Windows user (no admin rights).
rem Use this if the Santa-Setup .exe is blocked on your PC; it does the same:
rem copies Santa to %LOCALAPPDATA%\Programs\Santa, adds Start-menu, desktop and
rem sign-in shortcuts, creates Documents\Hiring Right\02_Batch_Videos and
rem 03_Transcripts, downloads the speech model and opens Santa.
setlocal
set "SRC=%~dp0app"
set "DEST=%LOCALAPPDATA%\Programs\Santa"
set "PY=%DEST%\python\python.exe"
title Installing Santa

if not exist "%SRC%\python\python.exe" (
  echo Could not find the "app" folder next to this file. Unzip the whole Santa folder first.
  pause & exit /b 1
)

echo == Installing Santa into %DEST%
if exist "%PY%" "%PY%" -m whisper_key.santa.cli --setup stop
if exist "%DEST%\python" rmdir /s /q "%DEST%\python"
robocopy "%SRC%" "%DEST%" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 (
  echo Copying failed. Is there enough free disk space ^(about 1 GB^)?
  pause & exit /b 1
)
rem Files unzipped from a download carry an "internet" mark that makes Windows
rem ask before each start; clear it for Santa's own folder only.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -LiteralPath '%DEST%' -Recurse | Unblock-File" >nul 2>&1

echo == Creating shortcuts
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$sh = New-Object -ComObject WScript.Shell;" ^
  "function Link($folder, $extra) { $l = $sh.CreateShortcut((Join-Path $folder 'Santa.lnk'));" ^
  "  $l.TargetPath = '%DEST%\python\pythonw.exe'; $l.Arguments = ('-m whisper_key.santa.cli ' + $extra).Trim();" ^
  "  $l.WorkingDirectory = '%DEST%'; $l.IconLocation = '%DEST%\santa.ico'; $l.Description = 'Santa: offline transcription'; $l.Save() }" ^
  "Link ([Environment]::GetFolderPath('Programs')) '';" ^
  "Link ([Environment]::GetFolderPath('Desktop')) '';" ^
  "Link ([Environment]::GetFolderPath('Startup')) '--no-browser'"

echo == Setting up the Hiring Right folders
"%PY%" -m whisper_key.santa.cli --setup hiring-right
echo == Downloading the speech model (once; can take several minutes)
"%PY%" -m whisper_key.santa.cli --setup model
echo == Checking for an NVIDIA graphics card
"%PY%" -m whisper_key.santa.cli --setup gpu

echo.
echo Santa is installed. It opens in your web browser now and shows a Santa icon
echo in the taskbar tray. Next time, use the Santa icon on the desktop or Start menu.
start "" "%DEST%\python\pythonw.exe" -m whisper_key.santa.cli
pause
