@echo off
cd /d "%~dp0"
if exist ".venv-build\Scripts\pythonw.exe" (
  start "" ".venv-build\Scripts\pythonw.exe" "lan_remote.py"
) else (
  start "" pythonw "lan_remote.py"
)
