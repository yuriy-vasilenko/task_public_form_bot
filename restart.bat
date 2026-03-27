@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo Stopping listener on port 8000...
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"
timeout /t 1 /nobreak >nul

if not exist ".venv\Scripts\python.exe" (
  echo Run start.bat first to create .venv
  pause
  exit /b 1
)
call ".venv\Scripts\activate.bat"
echo Starting http://127.0.0.1:8000 ...
python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload
pause
