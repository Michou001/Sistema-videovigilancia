<#
Prepara un equipo para correr GOSS IP con la carpeta copiada de la memoria.

    preparar_respaldo.bat

Se corre UNA vez en el equipo de respaldo (o al reinstalar). Necesita internet:
descarga unos 3 GB. Hace lo mismo que la instalacion del README:

  1. comprueba Python 3.13 (si falta, lo instala con winget);
  2. crea el entorno de Python (venv): el que se copia de otra PC no sirve,
     apunta al Python de ese equipo;
  3. instala torch con CUDA si hay GPU NVIDIA (o la version CPU si no);
  4. instala el resto de dependencias con las versiones probadas;
  5. comprueba GPU, .env, base de datos y que el programa cargue.

Se puede volver a correr sin riesgo: no toca .env, data/ ni los modelos.
#>
# Continue y no Stop: en Windows PowerShell 5.1 cualquier aviso de pip o
# python en stderr se volveria un error fatal. Los fallos se detectan por
# codigo de salida y se detienen con throw.
$ErrorActionPreference = 'Continue'
$Raiz = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Raiz
$VPy = Join-Path $Raiz 'venv\Scripts\python.exe'

function Paso([string]$texto) { Write-Host ''; Write-Host "== $texto" -ForegroundColor Cyan }
function Ejecutar([string]$exe, [string[]]$argumentos) {
    & $exe @argumentos
    if ($LASTEXITCODE -ne 0) { throw "Fallo: $exe $($argumentos -join ' ')" }
}
# Devuelve el ejecutable y sus argumentos previos (py -3.13, o python).
function Python313 {
    foreach ($c in @(@{Exe = 'py'; Pre = @('-3.13')}, @{Exe = 'python'; Pre = @()})) {
        try {
            $v = & $c.Exe @($c.Pre + @('-c', "import sys; print('%d.%d' % sys.version_info[:2])")) 2>$null
            if ($LASTEXITCODE -eq 0 -and $v -eq '3.13') { return $c }
        } catch { }
    }
    return $null
}

Write-Host '== GOSS IP: preparar equipo de respaldo ==' -ForegroundColor Cyan

# --- 1. Python -----------------------------------------------------------------
Paso 'Python 3.13'
$py = Python313
if (-not $py) {
    Write-Host 'No se encontro Python 3.13. Se instala con winget...'
    winget install -e --id Python.Python.3.13 --scope user --accept-package-agreements --accept-source-agreements
    Write-Host ''
    Write-Host 'Python instalado. Cierra esta ventana y vuelve a abrir preparar_respaldo.bat.' -ForegroundColor Yellow
    exit 1
}
Write-Host "  ok: $($py.Exe) $($py.Pre)"

# --- 2. Entorno de Python ------------------------------------------------------
Paso 'Entorno de Python (venv)'
if (Test-Path -LiteralPath $VPy) {
    & $VPy -c "import sys" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host '  El venv viene de otro equipo y no funciona aqui: se crea de nuevo.'
        Remove-Item -LiteralPath (Join-Path $Raiz 'venv') -Recurse -Force -ErrorAction Stop
    }
}
if (-not (Test-Path -LiteralPath $VPy)) {
    Ejecutar $py.Exe ($py.Pre + @('-m', 'venv', 'venv'))
}
Ejecutar $VPy @('-m', 'pip', 'install', '--upgrade', 'pip', '--quiet')

# --- 3. torch ------------------------------------------------------------------
Paso 'torch (motor de los modelos)'
$gpu = [bool](Get-Command nvidia-smi -ErrorAction SilentlyContinue)
if ($gpu) {
    Write-Host '  GPU NVIDIA detectada: torch con CUDA.'
    Ejecutar $VPy @('-m', 'pip', 'install', 'torch==2.13.0+cu126', 'torchvision==0.28.0+cu126', '--index-url', 'https://download.pytorch.org/whl/cu126')
} else {
    Write-Warning 'Sin GPU NVIDIA: se instala torch para CPU. Funciona, pero 5 a 10 veces mas lento.'
    Ejecutar $VPy @('-m', 'pip', 'install', 'torch', 'torchvision')
}

# --- 4. Dependencias -----------------------------------------------------------
Paso 'Dependencias del sistema'
Ejecutar $VPy @('-m', 'pip', 'install', '-r', 'requirements.txt', '-c', 'constraints.txt')
if ($gpu) {
    # insightface arrastra onnxruntime (CPU), que eclipsa a onnxruntime-gpu y
    # deja los rostros en CPU sin avisar (ver requirements.txt).
    $proveedores = & $VPy -c "import onnxruntime as o; print(','.join(o.get_available_providers()))"
    if ($proveedores -notmatch 'CUDAExecutionProvider') {
        & $VPy -m pip uninstall -y onnxruntime
        Ejecutar $VPy @('-m', 'pip', 'install', '--force-reinstall', '--no-deps', 'onnxruntime-gpu==1.22.0')
    }
}

# --- 5. Comprobaciones ---------------------------------------------------------
Paso 'Comprobaciones'
$avisos = 0
$cuda = & $VPy -c "import torch; print(torch.cuda.is_available())"
Write-Host "  GPU disponible para los modelos: $cuda"
if ($gpu -and $cuda -ne 'True') { Write-Warning 'Hay GPU pero torch no la ve: revisa el controlador de NVIDIA.'; $avisos++ }

foreach ($f in '.env', 'data\vigilancia.db') {
    if (Test-Path -LiteralPath (Join-Path $Raiz $f)) { Write-Host "  ok: $f" }
    else { Write-Warning "Falta $f : la carpeta no se copio completa de la memoria."; $avisos++ }
}
if (-not (Test-Path -LiteralPath (Join-Path $Raiz '.env.cam2'))) {
    Write-Host '  (sin .env.cam2: solo se usara la primera camara)'
}

& $VPy -c "import api.main, edge.worker" 2>$null
if ($LASTEXITCODE -eq 0) { Write-Host '  ok: el programa carga' }
else { Write-Warning 'El programa no carga: corre  venv\Scripts\python -c "import api.main"  para ver el error.'; $avisos++ }

Write-Host ''
if ($avisos -eq 0) { Write-Host 'Equipo listo.' -ForegroundColor Green }
else { Write-Host "Equipo preparado con $avisos aviso(s): revisalos arriba." -ForegroundColor Yellow }
Write-Host @'

Siguientes pasos:
  1. Conecta este equipo a la red de las camaras (cable Ethernet).
  2. Doble clic en iniciar_api.bat, iniciar_worker.bat e iniciar_worker_cam2.bat.
  3. La primera vez Windows pregunta si Python puede usar la red: elige "Permitir".
  4. Abre http://127.0.0.1:8000 y entra con tu cuenta.
'@
