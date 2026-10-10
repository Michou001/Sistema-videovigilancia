<#
Copia el sistema a una memoria USB para levantarlo en el equipo de respaldo.

    copiar_a_memoria.bat            (pregunta la letra de la memoria)
    copiar_a_memoria.bat E          (directo a E:)

Copia la carpeta completa con .env, base de datos, evidencias y modelos, pero
NO el entorno de Python (venv: no sirve en otra PC, se crea alla con
preparar_respaldo.bat) ni respaldos/ (pesa mucho y ya vive en OneDrive).

Detiene el sistema antes de copiar: una base de datos copiada mientras se
escribe puede quedar danada. Al final comprueba que la copia este completa y
que la base de datos de la memoria este integra.
#>
param([string]$Unidad)

# Continue y no Stop: en Windows PowerShell 5.1 cualquier aviso de pip o
# python en stderr se volveria un error fatal. Los fallos se detectan por
# codigo de salida y se detienen con throw.
$ErrorActionPreference = 'Continue'
$Raiz = Split-Path -Parent $PSScriptRoot
$Excluir = @('venv', '.venv', 'respaldos', '.claude', '__pycache__', '.pytest_cache', '.ruff_cache')

function Preguntar([string]$texto) {
    return (Read-Host "$texto (s/n)").Trim().ToLower().StartsWith('s')
}

Write-Host ''
Write-Host '== GOSS IP: copiar a la memoria de respaldo ==' -ForegroundColor Cyan

# --- 1. Memoria ----------------------------------------------------------------
if (-not $Unidad) {
    $memorias = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=2')
    if (-not $memorias) { throw 'No hay ninguna memoria USB conectada.' }
    foreach ($m in $memorias) {
        '  {0}  {1,-20} libre {2:N1} GB' -f $m.DeviceID, $m.VolumeName, ($m.FreeSpace / 1GB) | Write-Host
    }
    $Unidad = Read-Host 'Letra de la memoria (por ejemplo E)'
}
$Unidad = $Unidad.Trim().TrimEnd(':', '\') + ':'
if (-not (Test-Path "$Unidad\")) { throw "No existe la unidad $Unidad" }
if ($Raiz.StartsWith($Unidad, [StringComparison]::OrdinalIgnoreCase)) {
    throw "La unidad $Unidad es la misma donde esta el sistema."
}
$Destino = Join-Path "$Unidad\" 'GOSS_IP_respaldo\Sistema de Videovigilancia'

# La memoria lleva contrasenas de camaras, cuentas y referencias de rostros.
try {
    $estado = (New-Object -ComObject Shell.Application).NameSpace("$Unidad\").Self.ExtendedProperty('System.Volume.BitLockerProtection')
} catch { $estado = $null }
if ($estado -notin 1, 3, 5, 6) {
    Write-Warning "La memoria $Unidad NO esta cifrada con BitLocker."
    Write-Host '  Lleva contrasenas y datos personales: si se pierde, es una fuga de datos.'
    Write-Host '  Para cifrarla: clic derecho en la unidad > Activar BitLocker.'
    if (-not (Preguntar 'Copiar de todos modos?')) { exit 1 }
}

# --- 2. Espacio ----------------------------------------------------------------
Write-Host 'Calculando tamano...'
$patron = ($Excluir | ForEach-Object { [regex]::Escape("\$_\") }) -join '|'
$bytes = (Get-ChildItem -LiteralPath $Raiz -Recurse -File -Force -ErrorAction SilentlyContinue |
          Where-Object { $_.FullName.Substring($Raiz.Length) -notmatch $patron } |
          Measure-Object Length -Sum).Sum
$libre = (Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$Unidad'").FreeSpace
if (Test-Path -LiteralPath $Destino) {
    $libre += (Get-ChildItem -LiteralPath $Destino -Recurse -File -Force -ErrorAction SilentlyContinue |
               Measure-Object Length -Sum).Sum
}
'  A copiar: {0:N2} GB    libre en la memoria: {1:N2} GB' -f ($bytes / 1GB), ($libre / 1GB) | Write-Host
if ($bytes -gt $libre) { throw 'No cabe en la memoria.' }

# --- 3. Detener el sistema -----------------------------------------------------
$procesos = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
              Where-Object { $_.CommandLine -match '-m (api|edge\.worker)' -and
                             $_.ExecutablePath -and $_.ExecutablePath.StartsWith($Raiz) })
$estabaCorriendo = $procesos.Count -gt 0
if ($estabaCorriendo) {
    Write-Host 'El sistema esta encendido. Hay que detenerlo para copiar la base de datos sin danarla.'
    if (-not (Preguntar 'Detener el sistema ahora?')) { exit 1 }
    foreach ($p in $procesos) { taskkill /PID $p.ProcessId /T /F | Out-Null }
    Start-Sleep -Seconds 3
}

# --- 4. Copiar -----------------------------------------------------------------
Write-Host "Copiando a $Destino ..."
$opciones = @('/MIR', '/XJ', '/R:1', '/W:1', '/NFL', '/NDL', '/NP', '/XD') + $Excluir
robocopy $Raiz $Destino @opciones | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy fallo (codigo $LASTEXITCODE)." }

# --- 5. Verificar --------------------------------------------------------------
# Una segunda pasada en modo lista (/L) no debe encontrar nada pendiente.
robocopy $Raiz $Destino @opciones /L | Out-Null
$completa = $LASTEXITCODE -eq 0
$bd = Join-Path $Destino 'data\vigilancia.db'
$integridad = 'sin base de datos'
if (Test-Path -LiteralPath $bd) {
    $py = Join-Path $Raiz 'venv\Scripts\python.exe'
    $integridad = & $py -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('pragma integrity_check').fetchone()[0])" $bd
}
$fecha = Get-Date -Format 'yyyy-MM-dd HH:mm'
@"
GOSS IP - copia para el equipo de respaldo
Copiada: $fecha desde $env:COMPUTERNAME

En el equipo de respaldo:
 1. Copia la carpeta "Sistema de Videovigilancia" al disco (por ejemplo al Escritorio).
 2. La primera vez, doble clic en preparar_respaldo.bat (necesita internet).
 3. Doble clic en iniciar_api.bat, iniciar_worker.bat e iniciar_worker_cam2.bat.
 4. Abre http://127.0.0.1:8000 y entra con tu cuenta.

Esta memoria lleva contrasenas y datos personales: guardala bajo llave.
"@ | Set-Content -Encoding UTF8 -LiteralPath (Join-Path "$Unidad\" 'GOSS_IP_respaldo\LEEME.txt')

Write-Host ''
if ($completa -and $integridad -eq 'ok') {
    Write-Host "Copia completa y base de datos integra ($fecha)." -ForegroundColor Green
} else {
    Write-Host "Revisar: copia completa = $completa, base de datos = $integridad" -ForegroundColor Yellow
}

# --- 6. Volver a encender ------------------------------------------------------
if ($estabaCorriendo -and (Preguntar 'Volver a encender el sistema?')) {
    foreach ($bat in 'iniciar_api.bat', 'iniciar_worker.bat', 'iniciar_worker_cam2.bat') {
        if (Test-Path -LiteralPath (Join-Path $Raiz $bat)) {
            Start-Process -FilePath (Join-Path $Raiz $bat) -WorkingDirectory $Raiz
            if ($bat -eq 'iniciar_api.bat') { Start-Sleep -Seconds 8 }
        }
    }
}
