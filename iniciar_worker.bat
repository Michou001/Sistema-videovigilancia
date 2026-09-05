@echo off
REM Lanza el worker de borde usando SIEMPRE el Python del venv del proyecto,
REM con su torch+CUDA. Evita el problema de correr por accidente con el
REM Python global del sistema (que no tiene GPU y vuelve todo lentisimo).
setlocal
set VENV_PY=%~dp0venv\Scripts\python.exe

if not exist "%VENV_PY%" (
    echo [x] No existe %VENV_PY%
    echo     Crea el entorno primero: python -m venv venv
    pause
    exit /b 1
)

"%VENV_PY%" -m edge.worker %*
pause
