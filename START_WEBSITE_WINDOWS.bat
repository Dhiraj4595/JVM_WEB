@echo off
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py app_server.py --port 8000
) else (
  python app_server.py --port 8000
)
pause
