@echo off
rem Deskhand: start the perception servers if needed, then open the floating panel.
rem Set DESKHAND_LOCK to a file path to hold a desktop lock while the agent works.
cd /d "%~dp0"
".venv\Scripts\python.exe" scripts\stack.py up
start "" ".venv\Scripts\pythonw.exe" scripts\deskhand_ui.py %*
