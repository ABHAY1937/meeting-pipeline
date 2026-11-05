@echo off
REM Double-click: runs the recording set in run_meeting.py
REM Drag a recording onto this file: runs that recording instead
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if "%~1"=="" (
    python run_meeting.py
) else (
    python meeting_pipeline.py "%~1"
)
echo.
pause
