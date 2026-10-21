@echo off
chcp 65001 >nul
cd /d "%~dp0"
python installer.py
echo.
pause
