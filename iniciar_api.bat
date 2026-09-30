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

REM python -m api lee API_HOST, API_PORT y, si estan, SSL_CERTFILE/SSL_KEYFILE
REM del .env para servir por HTTPS (ver tools\generar_certificado.py).
"%VENV_PY%" -m api
pause
