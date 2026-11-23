# Amolex-9026 setup. Installs EVERYTHING under $InstallRoot (default E:\ProgramData\Amolex):
#   uv (package manager), Python 3.12, the virtual environment, speech models, and optionally Ollama.
# Safe to run again: finished steps are skipped.

param(
    [string]$InstallRoot = $(if ($env:AMOLEX_HOME) { $env:AMOLEX_HOME } else { "E:\ProgramData\Amolex" }),
    [string[]]$Models = @("gpt-oss:20b"),
    [string]$HeavyModel = "gpt-oss:20b",
    [switch]$SkipOllama
)

$ErrorActionPreference = "Stop"
$ProjectDir = $PSScriptRoot
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

New-Item -ItemType Directory -Force -Path $InstallRoot, "$InstallRoot\models\vosk", "$InstallRoot\models\whisper", "$InstallRoot\cache", "$InstallRoot\temp" | Out-Null

# Keep every tool's files inside InstallRoot
$env:UV_INSTALL_DIR        = "$InstallRoot\uv"
$env:UV_NO_MODIFY_PATH     = "1"
$env:UV_PYTHON_INSTALL_DIR = "$InstallRoot\python"
$env:UV_PYTHON_PREFERENCE  = "only-managed"
$env:UV_CACHE_DIR          = "$InstallRoot\cache\uv"
$env:UV_PROJECT_ENVIRONMENT = "$InstallRoot\venv"
$env:HF_HOME               = "$InstallRoot\cache\hf"
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"
$uv = "$InstallRoot\uv\uv.exe"

Step "uv package manager"
if (-not (Test-Path $uv)) {
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
}
& $uv --version

Step "Python 3.12 and packages (into $InstallRoot\venv)"
& $uv python install 3.12
& $uv sync --project "$ProjectDir" --python 3.12
if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }

Step "Vosk speech model"
$voskName = "vosk-model-small-en-us-0.15"
$voskDir = "$InstallRoot\models\vosk\$voskName"
if (-not (Test-Path $voskDir)) {
    $zip = "$InstallRoot\temp\$voskName.zip"
    Invoke-WebRequest "https://alphacephei.com/vosk/models/$voskName.zip" -OutFile $zip
    Expand-Archive $zip -DestinationPath "$InstallRoot\models\vosk" -Force
    Remove-Item $zip
}
Write-Host "Vosk model: $voskDir"

Step "Whisper model (downloads on first use, about 150 MB)"
& "$InstallRoot\venv\Scripts\python.exe" -c "from faster_whisper import WhisperModel; WhisperModel('base', device='cpu', compute_type='int8', download_root=r'$InstallRoot\models\whisper'); print('whisper ready')"

if (-not $SkipOllama) {
    Step "Ollama (the local brain; uses your AMD GPU when supported)"
    $ollamaDir = "$InstallRoot\ollama"
    $ollamaExe = "$ollamaDir\ollama.exe"
    $env:OLLAMA_MODELS = "$InstallRoot\models\ollama"
    if (-not (Test-Path $ollamaExe)) {
        $answer = Read-Host "Install Ollama into ${ollamaDir}? Amolex's brain runs on it (y/n)"
        if ($answer -match '^[yY]') {
            # Set the model folder BEFORE installing: the installer auto-starts Ollama, and a server
            # started without this would download models into C:\Users\<you>\.ollama instead
            [Environment]::SetEnvironmentVariable("OLLAMA_MODELS", "$InstallRoot\models\ollama", "User")
            $setup = "$InstallRoot\temp\OllamaSetup.exe"
            Invoke-WebRequest "https://ollama.com/download/OllamaSetup.exe" -OutFile $setup
            # Wait for the installer only: Start-Process -Wait would also wait for the Ollama app
            # the installer launches, which never exits on its own
            $installer = Start-Process $setup -ArgumentList "/DIR=`"$ollamaDir`"" -PassThru
            $installer.WaitForExit()
            Start-Sleep 2
            Remove-Item $setup -ErrorAction SilentlyContinue
        }
    }
    if (Test-Path $ollamaExe) {
        # Restart any running Ollama so it picks up the E: model folder (an auto-started one may not have it)
        Get-Process "ollama app", "ollama" -ErrorAction SilentlyContinue | Stop-Process -Force
        Start-Sleep 2
        Start-Process $ollamaExe -ArgumentList "serve" -WindowStyle Hidden
        Start-Sleep 3
        foreach ($m in $Models) { & $ollamaExe pull $m }   # main model (~13 GB; skipped if already there)
        $have = (& $ollamaExe list) -join "`n"
        if ($have -notmatch [regex]::Escape($HeavyModel)) {
            $answer = Read-Host "Also download the heavy model $HeavyModel for coding and writing (about 14 GB)? (y/n)"
            if ($answer -match '^[yY]') { & $ollamaExe pull $HeavyModel }
            else { Write-Host "Skipped. Heavy requests will use the normal model until you pull it." -ForegroundColor Yellow }
        }
    } else {
        Write-Host "Skipped. Amolex can't answer requests until Ollama is installed (fast commands still work)." -ForegroundColor Yellow
    }
}

Step "Done"
Write-Host "Next: start.bat --doctor, then start.bat"
