<#
Deskhand setup for a Windows 10/11 PC. From the Deskhand folder:

    powershell -ExecutionPolicy Bypass -File setup.ps1

Needs Python 3.12 (python.org) and git. An NVIDIA GPU is optional: without
one everything runs on the CPU, several times slower. Safe to run again; it
skips what is already done.

Options:
    -ModelsRepo OWNER/NAME   Hugging Face repo with the models (default
                             $env:DESKHAND_MODELS_REPO or GauravJ17/deskhand-models)
    -ModelsFrom DIR          copy the models from a local folder instead
    -Cpu                     use the CPU build even if there is an NVIDIA GPU
#>
param(
    [string]$ModelsRepo = "",
    [string]$ModelsFrom = "",
    [switch]$Cpu
)
# Native tools (pip, git) write warnings to stderr, which Windows PowerShell
# 5.1 turns into terminating errors under "Stop". Exit codes decide instead.
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
# StudioApprentice perception, at the commit Deskhand was tested with.
$PerceptionRepo = "https://github.com/KunalMGupta/StudioApprentice-perception"
$PerceptionCommit = "e6d32b028a4ab5b311ccc20bf2ea6ebb6d2f3914"
if (-not $ModelsRepo) {
    $ModelsRepo = if ($env:DESKHAND_MODELS_REPO) { $env:DESKHAND_MODELS_REPO } else { "GauravJ17/deskhand-models" }
}

function Step($m) { Write-Host "`n== $m" -ForegroundColor Cyan }
function Run([string]$exe, [string[]]$a) {
    & $exe @a
    if ($LASTEXITCODE -ne 0) { throw "failed ($LASTEXITCODE): $exe $($a -join ' ')" }
}

function Find-Python312 {
    # Where python.org's installer registers itself (PEP 514), then the usual
    # folders, then PATH. The Microsoft Store "python" stub is skipped: it
    # only opens the Store.
    $cands = @()
    foreach ($hive in "HKCU:", "HKLM:") {
        $k = "$hive\Software\Python\PythonCore\3.12\InstallPath"
        if (Test-Path $k) {
            $i = Get-ItemProperty $k
            if ($i.ExecutablePath) { $cands += $i.ExecutablePath }
            elseif ($i.'(default)') { $cands += (Join-Path $i.'(default)' "python.exe") }
        }
    }
    $cands += "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    $cands += "$env:ProgramFiles\Python312\python.exe"
    $g = Get-Command py -ErrorAction SilentlyContinue
    if ($g -and $g.Source -notlike "*WindowsApps*") {
        $p = cmd /c "py -3.12 -c `"import sys;print(sys.executable)`" 2>nul"
        if ($p) { $cands += $p.Trim() }
    }
    $g = Get-Command python -ErrorAction SilentlyContinue
    if ($g -and $g.Source -notlike "*WindowsApps*") { $cands += $g.Source }
    foreach ($p in $cands) {
        if ($p -and (Test-Path $p)) {
            $v = cmd /c "`"$p`" -c `"import sys;print('%d.%d' % sys.version_info[:2])`" 2>nul"
            if ($v -and $v.Trim() -eq "3.12") { return $p }
        }
    }
    return $null
}

Step "Folder path length"
# PyTorch's files nest about 160 characters deep; Windows refuses paths over
# 260 unless long paths are switched on. Measured: a 120-character folder failed.
$long = (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -ErrorAction SilentlyContinue).LongPathsEnabled -eq 1
if (-not $long -and $PSScriptRoot.Length -gt 90) {
    throw "This folder's path is $($PSScriptRoot.Length) characters long, and Windows long paths are off, so PyTorch cannot install here. Move Deskhand somewhere shorter (for example C:\Deskhand) and run this again."
}
Write-Host "ok ($($PSScriptRoot.Length) characters$(if ($long) { ', long paths on' }))"

Step "Python 3.12 and git"
$py = Find-Python312
if (-not $py) { throw "Python 3.12 not found. Install it from https://www.python.org/downloads/ and run this again." }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "git not found. Install it from https://git-scm.com/download/win and run this again." }
Write-Host "using $py"

Step "Virtual environment (.venv)"
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Run $py @("-m", "venv", ".venv")
}
Run $venvPy @("-m", "pip", "install", "-q", "--upgrade", "pip")

$gpu = (-not $Cpu) -and [bool](Get-Command nvidia-smi -ErrorAction SilentlyContinue)
Step ("PyTorch (" + $(if ($gpu) { "NVIDIA GPU, CUDA 12.8" } else { "CPU" }) + ")")
$index = if ($gpu) { "https://download.pytorch.org/whl/cu128" } else { "https://download.pytorch.org/whl/cpu" }
Run $venvPy @("-m", "pip", "install", "-q", "torch", "torchvision", "--index-url", $index)

Step "Python packages (requirements.txt)"
Run $venvPy @("-m", "pip", "install", "-q", "-r", "requirements.txt")
if ($gpu) {
    # rapidocr brings the CPU onnxruntime, which hides the GPU build: swap it.
    & $venvPy -m pip uninstall -y -q onnxruntime onnxruntime-gpu 2>&1 | Out-Null
    Run $venvPy @("-m", "pip", "install", "-q", "onnxruntime-gpu[cuda,cudnn]==1.28.0")
}

Step "StudioApprentice perception (repos\StudioApprentice-perception)"
$perc = Join-Path $PSScriptRoot "repos\StudioApprentice-perception"
if (-not (Test-Path (Join-Path $perc ".git"))) {
    Run "git" @("clone", "-q", $PerceptionRepo, $perc)
}
Run "git" @("-C", $perc, "fetch", "-q", "origin", $PerceptionCommit)
Run "git" @("-C", $perc, "checkout", "-q", $PerceptionCommit)
Run $venvPy @("-m", "pip", "install", "-q", "--no-deps", "-e", $perc)

Step "Models"
if ($ModelsFrom) { Run $venvPy @("scripts\fetch_models.py", "--from-dir", $ModelsFrom) }
else { Run $venvPy @("scripts\fetch_models.py", "--repo", $ModelsRepo) }

Step "OpenAI API key"
if ($env:OPENAI_API_KEY -or $env:DESKHAND_ENV_FILE -or (Test-Path ".env")) {
    Write-Host "found (environment or .env)"
} else {
    $sec = Read-Host "Paste your OpenAI API key (saved to .env in this folder, which git ignores)" -AsSecureString
    $key = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
    Set-Content -Path ".env" -Value "OPENAI_API_KEY=$key" -Encoding ascii
    Write-Host "saved to .env"
}

Step "Checking the models load"
Run $venvPy @("scripts\check_weights.py")

Write-Host "`nDeskhand is set up. Start it with Deskhand.cmd" -ForegroundColor Green
