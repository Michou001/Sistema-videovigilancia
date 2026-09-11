@echo off
REM Worker de la SEGUNDA camara: usa .env.cam2 en vez de .env, para no
REM pisar la configuracion de la primera. Corre junto con iniciar_worker.bat
REM (cam-01) y iniciar_api.bat -- los tres a la vez, cada uno en su terminal.
setlocal
set VENV_PY=%~dp0venv\Scripts\python.exe

if not exist "%VENV_PY%" (
    echo [x] No existe %VENV_PY%
    echo     Crea el entorno primero: python -m venv venv
    pause
    exit /b 1
)

"%VENV_PY%" -m edge.worker --env .env.cam2 %*
pause
