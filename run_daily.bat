@echo off
REM Daily NYC Concert Tracker runner — called by Windows Task Scheduler at 3:00 AM EST.
REM Activates the venv and runs the orchestrator script.

cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
call venv\Scripts\activate.bat
python run_daily.py
