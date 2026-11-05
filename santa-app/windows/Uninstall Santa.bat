@echo off
rem Uninstall Santa.bat - removes what "Install Santa.bat" created. Asks before
rem deleting settings and downloaded models. Never touches Documents\Hiring Right.
setlocal
set "DEST=%LOCALAPPDATA%\Programs\Santa"
title Uninstalling Santa
if exist "%DEST%\python\python.exe" "%DEST%\python\python.exe" -m whisper_key.santa.cli --setup stop
timeout /t 2 /nobreak >nul
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "foreach ($f in 'Programs','Desktop','Startup') { Remove-Item -LiteralPath (Join-Path ([Environment]::GetFolderPath($f)) 'Santa.lnk') -ErrorAction SilentlyContinue }"
if exist "%DEST%" rmdir /s /q "%DEST%"
echo Santa's program files are removed.

choice /m "Also delete Santa's settings and logs (%APPDATA%\Santa)"
if not errorlevel 2 rmdir /s /q "%APPDATA%\Santa"
choice /m "Also delete the downloaded speech models (0.5-3 GB)"
if not errorlevel 2 (
  for /d %%M in ("%USERPROFILE%\.cache\huggingface\hub\models--*faster-whisper*") do rmdir /s /q "%%M"
)
echo Done. Your Documents\Hiring Right folder was left as it is.
pause
