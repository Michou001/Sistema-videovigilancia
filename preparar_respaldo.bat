@echo off
REM Prepara el equipo de respaldo: Python, venv, torch y dependencias.
REM Se corre UNA vez en la PC de respaldo, con internet.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\preparar_respaldo.ps1"
pause
