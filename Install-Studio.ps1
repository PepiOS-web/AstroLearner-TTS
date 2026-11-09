param(
    [string]$Runtime = (Join-Path $env:LOCALAPPDATA 'AstroLearnerTTS'),
    [switch]$SkipModels
)
$ErrorActionPreference = 'Stop'
$ttsSourceRoot = $PSScriptRoot
$ttsRuntimeRoot = [IO.Path]::GetFullPath($Runtime)
$ttsVenvPython = Join-Path $ttsRuntimeRoot '.venv/Scripts/python.exe'
$ttsUv = Get-Command uv -ErrorAction Stop
$ttsPowerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
if (-not (Test-Path -LiteralPath (Join-Path $ttsSourceRoot 'requirements.lock') -PathType Leaf)) {
    throw 'Falta requirements.lock en la carpeta del proyecto.'
}
if (Get-NetTCPConnection -State Listen -LocalPort 8095 -ErrorAction SilentlyContinue) {
    throw 'Cierra AstroLearner TTS con su acceso de detener antes de instalar una actualización.'
}
New-Item -ItemType Directory -Force -Path $ttsRuntimeRoot | Out-Null
$env:UV_CACHE_DIR = Join-Path $ttsRuntimeRoot 'cache/uv'
if (-not (Test-Path -LiteralPath $ttsVenvPython)) {
    & $ttsUv.Source venv --python 3.12 (Join-Path $ttsRuntimeRoot '.venv')
    if ($LASTEXITCODE) { throw 'No se pudo crear el entorno Python 3.12.' }
}
& $ttsUv.Source pip install --python $ttsVenvPython 'torch==2.14.1+cu130' 'torchaudio==2.11.0+cu130' --index-url https://download.pytorch.org/whl/cu130
if ($LASTEXITCODE) { throw 'No se pudo instalar PyTorch CUDA.' }
& $ttsUv.Source pip install --python $ttsVenvPython --no-deps -r (Join-Path $ttsSourceRoot 'requirements.lock')
if ($LASTEXITCODE) { throw 'No se pudieron instalar las dependencias fijadas.' }
$ttsApp = Join-Path $ttsRuntimeRoot 'app'
New-Item -ItemType Directory -Force -Path $ttsApp | Out-Null
$ttsPackageSource = Join-Path $ttsSourceRoot 'astro_tts'
$ttsPackageTarget = Join-Path $ttsApp 'astro_tts'
Get-ChildItem -LiteralPath $ttsPackageSource -Recurse -File |
    Where-Object { $_.FullName -notmatch '[\\/]__pycache__[\\/]' -and $_.Extension -notin @('.pyc', '.pyo') } |
    ForEach-Object {
        $ttsRelativeFile = $_.FullName.Substring($ttsPackageSource.Length).TrimStart([char[]]'\/')
        $ttsTargetFile = Join-Path $ttsPackageTarget $ttsRelativeFile
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ttsTargetFile) | Out-Null
        Copy-Item -LiteralPath $_.FullName -Destination $ttsTargetFile -Force
    }
foreach ($ttsScript in @('Start-Studio.ps1','Stop-Studio.ps1')) {
    Copy-Item -LiteralPath (Join-Path $ttsSourceRoot $ttsScript) -Destination (Join-Path $ttsRuntimeRoot $ttsScript) -Force
}
New-Item -ItemType Directory -Force -Path (Join-Path $ttsRuntimeRoot 'bin') | Out-Null
if (-not (Test-Path -LiteralPath (Join-Path $ttsRuntimeRoot 'bin/ffmpeg.exe'))) {
    $ttsFfmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($ttsFfmpeg) {
        Copy-Item -LiteralPath $ttsFfmpeg.Source -Destination (Join-Path $ttsRuntimeRoot 'bin/ffmpeg.exe')
    } else {
        Write-Warning 'FFmpeg no está disponible: se conservarán los WAV originales sin máster de sonoridad.'
    }
}
if (-not $SkipModels) {
    & $ttsVenvPython (Join-Path $ttsSourceRoot 'scripts/download_models.py') --runtime $ttsRuntimeRoot
    if ($LASTEXITCODE) { throw 'La descarga de los modelos no se ha completado.' }
}
$ttsDesktop = [Environment]::GetFolderPath('Desktop')
$ttsShell = New-Object -ComObject WScript.Shell
foreach ($ttsEntry in @(
    @{Name='AstroLearner TTS'; Script='Start-Studio.ps1'},
    @{Name='Detener AstroLearner TTS'; Script='Stop-Studio.ps1'}
)) {
    $ttsShortcut = $ttsShell.CreateShortcut((Join-Path $ttsDesktop ($ttsEntry.Name + '.lnk')))
    $ttsShortcut.TargetPath = $ttsPowerShell
    $ttsShortcut.Arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + (Join-Path $ttsRuntimeRoot $ttsEntry.Script) + '"'
    $ttsShortcut.WorkingDirectory = $ttsRuntimeRoot
    $ttsShortcut.Description = $ttsEntry.Name + ' · estudio local de voces'
    $ttsShortcut.Save()
}
Write-Output ('Instalación preparada en ' + $ttsRuntimeRoot)
Write-Output 'Abre el acceso AstroLearner TTS del escritorio. Las voces necesitan escucha y aprobación.'
