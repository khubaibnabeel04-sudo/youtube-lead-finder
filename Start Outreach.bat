@echo off
cd /d "%~dp0"

start "Backend" cmd /k "venv\Scripts\python.exe backend\main.py"
start "Frontend" cmd /k "cd frontend && npm start"

timeout /t 5 /nobreak >nul
start "" "http://localhost:3000"
