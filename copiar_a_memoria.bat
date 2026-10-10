@echo off
REM Copia el sistema a una memoria USB para el equipo de respaldo.
REM Detiene el sistema, copia sin venv ni respaldos y verifica la copia.
REM Uso: doble clic, o  copiar_a_memoria.bat E
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\copiar_a_memoria.ps1" %*
pause
