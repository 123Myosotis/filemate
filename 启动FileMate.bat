@echo off
cd /d "%~dp0"

where pyw >nul 2>&1
if not errorlevel 1 goto usepyw
where pythonw >nul 2>&1
if not errorlevel 1 goto usepythonw
where py >nul 2>&1
if not errorlevel 1 goto usepy
where python >nul 2>&1
if not errorlevel 1 goto usepython

echo [FileMate] Python not found. Install Python 3 and add it to PATH.
pause
exit /b 1

:usepyw
pyw -3 -c "import PyQt6" >nul 2>&1
if errorlevel 1 (
  py -3 -m pip install -r "%~dp0requirements.txt"
  if errorlevel 1 goto fail
)
start "" pyw -3 "%~dp0main.py"
exit /b 0

:usepythonw
pythonw -c "import PyQt6" >nul 2>&1
if errorlevel 1 (
  python -m pip install -r "%~dp0requirements.txt"
  if errorlevel 1 goto fail
)
start "" pythonw "%~dp0main.py"
exit /b 0

:usepy
py -3 -c "import PyQt6" >nul 2>&1
if errorlevel 1 (
  py -3 -m pip install -r "%~dp0requirements.txt"
  if errorlevel 1 goto fail
)
start "" pyw -3 "%~dp0main.py" 2>nul
if errorlevel 1 start "" py -3 "%~dp0main.py"
exit /b 0

:usepython
python -c "import PyQt6" >nul 2>&1
if errorlevel 1 (
  python -m pip install -r "%~dp0requirements.txt"
  if errorlevel 1 goto fail
)
start "" pythonw "%~dp0main.py" 2>nul
if errorlevel 1 start "" python "%~dp0main.py"
exit /b 0

:fail
echo [FileMate] Dependency install failed. Run: py -3 -m pip install -r requirements.txt
pause
exit /b 1
