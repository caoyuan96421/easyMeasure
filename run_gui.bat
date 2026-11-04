@echo off
rem Start the GUI; launches a detached backend (restoring the last setup) if none is running.
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" -m easymeasure.frontend --start-backend
