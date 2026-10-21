@echo off
rem Deskhand: start the perception servers if needed, then open the floating panel.
rem Optional: DESKHAND_ENV_FILE (where OPENAI_API_KEY is), DESKHAND_LOCK (a desktop lock file).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (echo Run setup.ps1 first. & pause & exit /b 1)
".venv\Scripts\python.exe" scripts\stack.py up
start "" ".venv\Scripts\pythonw.exe" scripts\deskhand_ui.py %*
