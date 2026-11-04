@echo off
rem Create .venv next to this file and install easyMeasure (editable) with
rem hardware + test extras.  Re-running it just updates the installation.
setlocal
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    if exist "%USERPROFILE%\miniconda3\python.exe" set "PY=%USERPROFILE%\miniconda3\python.exe"
)
if not defined PY (
    echo No Python found. Install Python 3.9+ from python.org and run this again.
    pause
    exit /b 1
)

echo Using: %PY%
%PY% --version
if not exist ".venv\Scripts\python.exe" (
    echo Creating .venv ...
    %PY% -m venv .venv || goto :fail
)
".venv\Scripts\python.exe" -m pip install --upgrade pip || goto :fail
".venv\Scripts\python.exe" -m pip install -e ".[hardware,test]" || goto :fail
".venv\Scripts\python.exe" -c "import easymeasure, PySide6, pyqtgraph, h5py, pptx; print('easyMeasure', easymeasure.__version__, 'ready')" || goto :fail

echo.
echo Done. Try:
echo   .venv\Scripts\python -m easymeasure.backend --setup examples\demo_setup.json
echo   .venv\Scripts\python -m easymeasure.frontend
echo or simply  run_gui.bat
pause
exit /b 0

:fail
echo.
echo Setup FAILED - see the messages above.
pause
exit /b 1
