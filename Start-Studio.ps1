param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$ttsRuntimeRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$ttsPython = [IO.Path]::GetFullPath((Join-Path $ttsRuntimeRoot '.venv/Scripts/python.exe'))
$ttsPidFile = Join-Path $ttsRuntimeRoot 'studio.pid'
$ttsHealthUrl = 'http://127.0.0.1:8095/api/health'
$ttsStudioUrl = 'http://127.0.0.1:8095/'
$ttsNewProcess = $null
$ttsNewCreationTicks = $null
$ttsOwnPidJson = $null
$ttsStartupReady = $false
$ttsMutex = $null
$ttsMutexOwned = $false

function Test-SamePath([string]$Left, [string]$Right) {
    if (-not $Left -or -not $Right) { return $false }
    return [string]::Equals(
        [IO.Path]::GetFullPath($Left).TrimEnd([char[]]'\/'),
        [IO.Path]::GetFullPath($Right).TrimEnd([char[]]'\/'),
        [StringComparison]::OrdinalIgnoreCase)
}

function Get-StudioHealth {
    try {
        $ttsReply = Invoke-RestMethod -Uri $ttsHealthUrl -TimeoutSec 8
        if ($ttsReply.app -eq 'AstroLearnerTTS' -and $ttsReply.ok -eq $true -and
            (Test-SamePath $ttsReply.doctor.runtime_dir $ttsRuntimeRoot)) { return $ttsReply }
    } catch { }
    return $null
}

try {
    $ttsHasher = [Security.Cryptography.SHA256]::Create()
    try { $ttsHash = [BitConverter]::ToString($ttsHasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($ttsRuntimeRoot.ToLowerInvariant()))).Replace('-', '') }
    finally { $ttsHasher.Dispose() }
    $ttsMutex = New-Object Threading.Mutex($false, ('Local\AstroLearnerTTS-' + $ttsHash))
    try { $ttsMutexOwned = $ttsMutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $ttsMutexOwned = $true }
    if (-not $ttsMutexOwned) { throw 'El estudio ya se está iniciando o deteniendo. Espera unos segundos.' }
    $ttsHealth = Get-StudioHealth
    if (-not $ttsHealth) {
        if (Get-NetTCPConnection -State Listen -LocalPort 8095 -ErrorAction SilentlyContinue) {
            throw 'El puerto 8095 lo ocupa otro programa o una instalación distinta. Revisa el diagnóstico antes de iniciar.'
        }
        if (-not (Test-Path -LiteralPath $ttsPython)) {
            throw 'Falta el entorno de Python. Ejecuta Install-Studio.ps1 desde la carpeta del proyecto.'
        }
        $ttsLogs = Join-Path $ttsRuntimeRoot 'logs'
        New-Item -ItemType Directory -Force -Path $ttsLogs | Out-Null
        $env:PYTHONPATH = Join-Path $ttsRuntimeRoot 'app'
        $env:HF_HOME = Join-Path $ttsRuntimeRoot 'cache/huggingface'
        $env:HF_HUB_OFFLINE = '1'
        $env:TRANSFORMERS_OFFLINE = '1'
        $env:HF_HUB_DISABLE_TELEMETRY = '1'
        $env:HF_HUB_DISABLE_IMPLICIT_TOKEN = '1'
        $env:PATH = (Join-Path $ttsRuntimeRoot 'bin') + [IO.Path]::PathSeparator + $env:PATH
        $ttsArguments = @('-u', '-m', 'astro_tts', '--runtime', ('"' + $ttsRuntimeRoot + '"'), '--port', '8095')
        $ttsNewProcess = Start-Process -FilePath $ttsPython -ArgumentList $ttsArguments `
            -WorkingDirectory (Join-Path $ttsRuntimeRoot 'app') -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $ttsLogs 'studio.out.log') `
            -RedirectStandardError (Join-Path $ttsLogs 'studio.err.log')
        $ttsNewCreationTicks = $ttsNewProcess.StartTime.ToUniversalTime().Ticks
        $ttsProcessInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$($ttsNewProcess.Id)"
        if (-not $ttsProcessInfo -or -not (Test-SamePath $ttsProcessInfo.ExecutablePath $ttsPython)) {
            throw 'No se pudo identificar el proceso del estudio. Consulta logs/studio.err.log.'
        }
        $ttsOwnPidJson = @{
            pid = $ttsNewProcess.Id
            executable = $ttsPython
            runtime = $ttsRuntimeRoot
            creation_utc_ticks = [string]$ttsProcessInfo.CreationDate.ToUniversalTime().Ticks
        } | ConvertTo-Json -Compress
        Set-Content -LiteralPath $ttsPidFile -Value $ttsOwnPidJson -Encoding UTF8
        $ttsDeadline = [DateTime]::UtcNow.AddSeconds(45)
        do {
            Start-Sleep -Milliseconds 600
            $ttsHealth = Get-StudioHealth
            $ttsNewProcess.Refresh()
            if ($ttsNewProcess.HasExited) { throw 'El estudio no ha podido arrancar. Consulta logs/studio.err.log.' }
        } until ($ttsHealth -or [DateTime]::UtcNow -gt $ttsDeadline)
        if (-not $ttsHealth) { throw 'El estudio no responde. Consulta logs/studio.err.log.' }
    }
    $ttsStartupReady = $true
    if (-not $NoBrowser) { Start-Process -FilePath $ttsStudioUrl }
    Write-Output $ttsStudioUrl
} catch {
    $ttsMessage = $_.Exception.Message
    if ($ttsNewProcess -and -not $ttsStartupReady) {
        try {
            $ttsCurrentProcess = Get-Process -Id $ttsNewProcess.Id -ErrorAction SilentlyContinue
            $ttsCurrentInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$($ttsNewProcess.Id)" -ErrorAction SilentlyContinue
            if ($ttsCurrentProcess -and $ttsNewCreationTicks -and
                $ttsCurrentProcess.StartTime.ToUniversalTime().Ticks -eq $ttsNewCreationTicks -and
                $ttsCurrentInfo -and (Test-SamePath $ttsCurrentInfo.ExecutablePath $ttsPython)) {
                & (Join-Path $env:SystemRoot 'System32/taskkill.exe') /PID $ttsNewProcess.Id /T /F 2>&1 | Out-Null
                if ($LASTEXITCODE -ne 0 -and (Get-CimInstance Win32_Process -Filter "ProcessId=$($ttsNewProcess.Id)")) {
                    throw 'El proceso recién iniciado sigue activo.'
                }
            }
            if ($ttsOwnPidJson -and (Test-Path -LiteralPath $ttsPidFile -PathType Leaf) -and
                (Get-Content -LiteralPath $ttsPidFile -Raw).Trim() -ceq $ttsOwnPidJson) {
                Remove-Item -LiteralPath $ttsPidFile -Force
            }
        } catch { $ttsMessage += ' No se pudo completar la limpieza; revisa el proceso registrado y los logs.' }
    }
    if ($NoBrowser) { throw $ttsMessage }
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($ttsMessage, 'AstroLearner TTS') | Out-Null
    exit 1
} finally {
    if ($ttsMutexOwned) { $ttsMutex.ReleaseMutex() }
    if ($ttsMutex) { $ttsMutex.Dispose() }
}
