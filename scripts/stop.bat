@echo off
REM Detiene Quiosco
cd /d "%~dp0\.."
docker compose down
