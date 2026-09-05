@echo off
REM Lanza el dashboard/API usando SIEMPRE el Python del venv del proyecto.
setlocal
set VENV_PY=%~dp0venv\Scripts\python.exe

if not exist "%VENV_PY%" (
    echo [x] No existe %VENV_PY%
    echo     Crea el entorno primero: python -m venv venv
    pause
    exit /b 1
)

"%VENV_PY%" -m uvicorn api.main:app --host 0.0.0.0 --port 8000
pause
