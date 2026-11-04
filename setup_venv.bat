@echo off
rem Create .venv next to this file and install easyMeasure (editable) with
rem hardware + test extras.  Re-running it just updates the installation.
rem
rem Python is searched in this order (each candidate is test-run, so the
rem Microsoft Store "python" alias stub is skipped automatically):
rem   1. the path given as first argument:  setup_venv.bat C:\path\to\python.exe
rem   2. the PYTHON environment variable
rem   3. the py launcher, python on PATH
rem   4. common install locations (python.org, Anaconda/Miniconda, ProgramData)
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "PY="
if not "%~1"=="" call :try "%~1"
if not defined PY if defined PYTHON call :try "%PYTHON%"
if not defined PY (
    where py >nul 2>nul && ( py -3 -c "import sys; assert sys.version_info >= (3, 9)" >nul 2>nul && set "PY=py -3" )
)
if not defined PY (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        if not defined PY call :try "%%P"
    )
)
if not defined PY (
    for %%D in ("%USERPROFILE%\miniconda3" "%USERPROFILE%\anaconda3" "%LOCALAPPDATA%\miniconda3" "%LOCALAPPDATA%\anaconda3" "%ProgramData%\miniconda3" "%ProgramData%\anaconda3" "C:\miniconda3" "C:\anaconda3") do (
        if not defined PY if exist "%%~D\python.exe" call :try "%%~D\python.exe"
    )
)
if not defined PY (
    for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*" "%ProgramFiles%\Python3*" "C:\Python3*") do (
        if not defined PY if exist "%%~D\python.exe" call :try "%%~D\python.exe"
    )
)
if not defined PY (
    echo.
    echo Could not find a working Python 3.9+.
    echo Run this script with the full path of your python.exe, for example:
    echo     setup_venv.bat C:\Users\%USERNAME%\miniconda3\python.exe
    echo ^(type "where python" in an Anaconda Prompt or terminal to find it^)
    pause
    exit /b 1
)

echo Using Python: %PY%
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
echo   run_demo_backend.bat      (backend with simulated instruments)
echo   run_gui.bat               (the GUI)
echo   .venv\Scripts\python -m pytest -q tests
pause
exit /b 0

:try
rem accept a candidate only if it really runs and is Python 3.9+
"%~1" -c "import sys, venv; assert sys.version_info >= (3, 9)" >nul 2>nul && set "PY="%~1""
exit /b 0

:fail
echo.
echo Setup FAILED - see the messages above.
pause
exit /b 1
