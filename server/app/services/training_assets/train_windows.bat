@echo off
setlocal
cd /d "%~dp0"
title Pigeon Tracker Model Trainer

if not exist "train_model.py" (
  echo ERROR: train_model.py is missing from this folder.
  pause
  exit /b 2
)

py -3.11 -c "import ensurepip, tkinter, venv" >nul 2>&1
if not errorlevel 1 (
  py -3.11 "train_model.py"
  goto finished
)

python --version >nul 2>&1
if not errorlevel 1 (
  python "train_model.py"
  goto finished
)

echo No Python runtime was found. The launcher will install standard Python 3.11.
where winget >nul 2>&1
if errorlevel 1 (
  echo ERROR: Windows Package Manager is unavailable.
  echo Install 64-bit Python 3.11 or newer from https://www.python.org/downloads/windows/
  echo Enable "Add python.exe to PATH", then run this launcher again.
  pause
  exit /b 2
)

winget install --exact --id Python.Python.3.11 --scope user --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto failed

set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python311\python.exe"
if not exist "%PYTHON_EXE%" (
  echo Python was installed. Close this window and run train_windows.bat again.
  pause
  exit /b 0
)
"%PYTHON_EXE%" "train_model.py"

:finished
if errorlevel 1 goto failed
exit /b 0

:failed
echo.
echo The trainer stopped with an error. Read the message above for details.
pause
exit /b 1
