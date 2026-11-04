@echo off
rem Start a backend in this console with the simulated demo instruments.
rem Use examples\faulty_setup.json instead to try the fault-injecting instruments.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m easymeasure.backend --setup examples\demo_setup.json %*
pause
