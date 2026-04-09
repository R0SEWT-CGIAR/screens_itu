@echo off
REM Inicia Quiosco en Docker
cd /d "%~dp0\.."
docker compose up -d --build
