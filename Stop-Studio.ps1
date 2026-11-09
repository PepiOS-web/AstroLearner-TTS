param([switch]$NoUI)
$ErrorActionPreference = 'Stop'
$ttsRuntimeRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$ttsPython = [IO.Path]::GetFullPath((Join-Path $ttsRuntimeRoot '.venv/Scripts/python.exe'))
$ttsPidFile = Join-Path $ttsRuntimeRoot 'studio.pid'
$ttsMutex = $null
$ttsMutexOwned = $false

function Test-SamePath([string]$Left, [string]$Right) {
    if (-not $Left -or -not $Right) { return $false }
    return [string]::Equals(
        [IO.Path]::GetFullPath($Left).TrimEnd([char[]]'\/'),
        [IO.Path]::GetFullPath($Right).TrimEnd([char[]]'\/'),
        [StringComparison]::OrdinalIgnoreCase)
}

try {
    $ttsHasher = [Security.Cryptography.SHA256]::Create()
    try { $ttsHash = [BitConverter]::ToString($ttsHasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($ttsRuntimeRoot.ToLowerInvariant()))).Replace('-', '') }
    finally { $ttsHasher.Dispose() }
    $ttsMutex = New-Object Threading.Mutex($false, ('Local\AstroLearnerTTS-' + $ttsHash))
    try { $ttsMutexOwned = $ttsMutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $ttsMutexOwned = $true }
    if (-not $ttsMutexOwned) { throw 'El estudio ya se está iniciando o deteniendo. Espera unos segundos.' }
    if (-not (Test-Path -LiteralPath $ttsPidFile -PathType Leaf)) {
        Write-Output 'El estudio no tiene un proceso registrado.'
        return
    }
    $ttsPidText = (Get-Content -LiteralPath $ttsPidFile -Raw).Trim()
    $ttsProcessId = 0
    $ttsLegacyPid = [int]::TryParse($ttsPidText, [ref]$ttsProcessId)
    $ttsRecord = $null
    if (-not $ttsLegacyPid) {
        $ttsRecord = $ttsPidText | ConvertFrom-Json
        $ttsProcessId = [int]$ttsRecord.pid
        $ttsCreationTicks = 0L
        if (-not (Test-SamePath $ttsRecord.executable $ttsPython) -or
            -not (Test-SamePath $ttsRecord.runtime $ttsRuntimeRoot) -or
            -not [long]::TryParse([string]$ttsRecord.creation_utc_ticks, [ref]$ttsCreationTicks)) {
            throw 'El registro del proceso no corresponde a esta instalación; no se ha detenido.'
        }
    }
    if ($ttsProcessId -le 0) { throw 'El registro del proceso no es válido; no se ha detenido.' }
    $ttsProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$ttsProcessId"
    if (-not $ttsProcess) {
        Remove-Item -LiteralPath $ttsPidFile -Force
        Write-Output 'El estudio ya estaba detenido.'
        return
    }
    # A legacy numeric PID requires the same exact interpreter and invocation.
    $ttsCommandPattern = '^\s*"?' + [regex]::Escape($ttsPython) + '"?\s+(?:-u\s+)?-m\s+astro_tts\s+--runtime\s+"?' + [regex]::Escape($ttsRuntimeRoot) + '"?\s+--port\s+8095\s*$'
    if (-not (Test-SamePath $ttsProcess.ExecutablePath $ttsPython) -or
        $ttsProcess.CommandLine -notmatch $ttsCommandPattern -or
        (-not $ttsLegacyPid -and $ttsProcess.CreationDate.ToUniversalTime().Ticks -ne $ttsCreationTicks)) {
        throw 'El identificador corresponde a otro proceso; no se ha detenido.'
    }
    # Windows venv launchers can keep a child interpreter alive: stop the owned tree.
    & (Join-Path $env:SystemRoot 'System32/taskkill.exe') /PID $ttsProcessId /T /F 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0 -and (Get-CimInstance Win32_Process -Filter "ProcessId=$ttsProcessId")) {
        throw 'No se pudo detener el estudio. Revisa logs y vuelve a intentarlo.'
    }
    Remove-Item -LiteralPath $ttsPidFile -Force
    Write-Output 'Estudio detenido. Los audios y las voces se conservan.'
} catch {
    if ($NoUI) { throw }
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'AstroLearner TTS') | Out-Null
    exit 1
} finally {
    if ($ttsMutexOwned) { $ttsMutex.ReleaseMutex() }
    if ($ttsMutex) { $ttsMutex.Dispose() }
}
