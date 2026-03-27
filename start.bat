@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Creating .venv...
  python -m venv .venv
  if errorlevel 1 (
    echo Python not found. Install Python 3 and add it to PATH.
    pause
    exit /b 1
  )
)

call ".venv\Scripts\activate.bat"
python -m pip install -r requirements.txt -q
echo.
echo Open http://127.0.0.1:8000  ^(Ctrl+C to stop^)
echo.
python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload

pause
