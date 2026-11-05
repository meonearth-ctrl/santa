; santa-app/windows/installer.nsi
; Santa for Windows installer (NSIS 3). Per-user: installs into
; %LOCALAPPDATA%\Programs\Santa without admin rights, adds Start-menu and
; desktop shortcuts and an "Apps & features" entry with an uninstaller.
; After copying it runs Santa's own set-up steps (Hiring Right folders, model
; download, optional NVIDIA GPU pack). Built by build_windows.py, which passes
; -DVERSION, -DPAYLOAD (folder with python\ + santa.ico) and -DOUTFILE.

Unicode true
!include "MUI2.nsh"
!include "LogicLib.nsh"

!define APP "Santa"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\Santa"
!define PYW '"$INSTDIR\python\pythonw.exe"'
!define PY '"$INSTDIR\python\python.exe"'
!define RUN_ARGS "-m whisper_key.santa.cli"

Name "${APP}"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\Programs\Santa"
InstallDirRegKey HKCU "Software\Santa" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
BrandingText "Santa ${VERSION} - offline transcription"
VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "Santa"
VIAddVersionKey "FileDescription" "Santa for Windows installer"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "LegalCopyright" "MIT licence, based on whisper-local by Rohit Burani"

; ── Pages ────────────────────────────────────────────────────────────────────
!define MUI_ICON "${PAYLOAD}\santa.ico"
!define MUI_UNICON "${PAYLOAD}\santa.ico"
!define MUI_WELCOMEPAGE_TITLE "Install Santa"
!define MUI_WELCOMEPAGE_TEXT "Santa turns interview recordings into transcripts on this PC. Videos never leave the computer.$\r$\n$\r$\nNo administrator rights are needed. The speech model is downloaded once during set-up, so stay connected to the internet."
!define MUI_COMPONENTSPAGE_SMALLDESC
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Open Santa now"
!define MUI_FINISHPAGE_RUN_FUNCTION LaunchSanta
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

; ── Sections ─────────────────────────────────────────────────────────────────
Section "Santa (required)" SecCore
  SectionIn RO
  ; An older Santa may be running from this folder; stop it so files can be replaced.
  ${If} ${FileExists} "$INSTDIR\python\python.exe"
    nsExec::ExecToLog '${PY} ${RUN_ARGS} --setup stop'
    Sleep 2000
  ${EndIf}
  RMDir /r "$INSTDIR\python"
  SetOutPath "$INSTDIR"
  File /r "${PAYLOAD}\*"
  WriteUninstaller "$INSTDIR\Uninstall Santa.exe"

  CreateShortcut "$SMPROGRAMS\Santa.lnk" ${PYW} "${RUN_ARGS}" "$INSTDIR\santa.ico" 0 SW_SHOWNORMAL "" "Santa: offline transcription"
  CreateShortcut "$DESKTOP\Santa.lnk" ${PYW} "${RUN_ARGS}" "$INSTDIR\santa.ico" 0 SW_SHOWNORMAL "" "Santa: offline transcription"

  WriteRegStr HKCU "Software\Santa" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "Santa (offline transcription)"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "Kout Food Group HR (Santa, based on whisper-local)"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\santa.ico"
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\Uninstall Santa.exe"'
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "EstimatedSize" 360000

  DetailPrint "Creating the Hiring Right folders in Documents..."
  nsExec::ExecToLog '${PY} ${RUN_ARGS} --setup hiring-right'
SectionEnd

Section "Download the speech model now (0.5-1.6 GB, once)" SecModel
  DetailPrint "Downloading the speech model. A window shows the progress..."
  ExecWait '${PY} ${RUN_ARGS} --setup model' $0
  ${If} $0 != 0
    MessageBox MB_ICONINFORMATION|MB_OK "The speech model could not be downloaded now. Santa will download it the first time it starts (internet needed once)." /SD IDOK
  ${EndIf}
SectionEnd

Section "Use an NVIDIA graphics card if this PC has one (about 1.2 GB extra)" SecGpu
  DetailPrint "Checking for an NVIDIA graphics card..."
  ExecWait '${PY} ${RUN_ARGS} --setup gpu'
SectionEnd

Section "Start Santa automatically when I sign in" SecStartup
  ; Watch-folder mode only works while Santa runs, so this is on by default.
  CreateShortcut "$SMSTARTUP\Santa.lnk" ${PYW} "${RUN_ARGS} --no-browser" "$INSTDIR\santa.ico" 0 SW_SHOWMINIMIZED
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecCore} "Santa itself, with its own Python. Creates Documents\Hiring Right\02_Batch_Videos and 03_Transcripts."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecModel} "Download the Whisper speech model during set-up instead of at the first transcription."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecGpu} "Only does something on PCs with an NVIDIA card: transcription becomes several times faster."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecStartup} "Santa starts quietly in the tray at sign-in, so new videos are transcribed without opening it."
!insertmacro MUI_FUNCTION_DESCRIPTION_END

Function LaunchSanta
  Exec '${PYW} ${RUN_ARGS}'
FunctionEnd

; ── Uninstaller ──────────────────────────────────────────────────────────────
Section "Uninstall"
  nsExec::ExecToLog '${PY} ${RUN_ARGS} --setup stop'
  Sleep 2000
  Delete "$SMPROGRAMS\Santa.lnk"
  Delete "$DESKTOP\Santa.lnk"
  Delete "$SMSTARTUP\Santa.lnk"
  RMDir /r "$INSTDIR\python"
  Delete "$INSTDIR\santa.ico"
  Delete "$INSTDIR\VERSION.txt"
  Delete "$INSTDIR\LICENSE.txt"
  Delete "$INSTDIR\Uninstall Santa.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "${UNINST_KEY}"
  DeleteRegKey HKCU "Software\Santa"

  ; Settings/logs and the downloaded models are kept unless the user says otherwise.
  ; The Hiring Right folders (videos, transcripts) are never touched.
  MessageBox MB_YESNO|MB_ICONQUESTION "Also delete Santa's settings and logs ($APPDATA\Santa)?" /SD IDNO IDNO keep_settings
    RMDir /r "$APPDATA\Santa"
  keep_settings:
  MessageBox MB_YESNO|MB_ICONQUESTION "Also delete the downloaded speech models (about 0.5-3 GB in $PROFILE\.cache\huggingface)?" /SD IDNO IDNO keep_models
    RMDir /r "$PROFILE\.cache\huggingface\hub\models--mobiuslabsgmbh--faster-whisper-large-v3-turbo"
    RMDir /r "$PROFILE\.cache\huggingface\hub\models--Systran--faster-whisper-small"
    RMDir /r "$PROFILE\.cache\huggingface\hub\models--Systran--faster-whisper-medium"
    RMDir /r "$PROFILE\.cache\huggingface\hub\models--Systran--faster-whisper-base"
    RMDir /r "$PROFILE\.cache\huggingface\hub\models--Systran--faster-whisper-large-v3"
  keep_models:
SectionEnd
