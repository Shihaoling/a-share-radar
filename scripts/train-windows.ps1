#requires -Version 5.1
<#
.SYNOPSIS
Run offline A-share experiments and a localhost progress dashboard on Windows.
.EXAMPLE
.\scripts\train-windows.ps1 -InstallCudaTorch -Device cuda
.EXAMPLE
.\scripts\train-windows.ps1 -Device cpu -MaxStocks 500 -Epochs 4 -Tune:$false
.NOTES
Run from a cloned repository containing data/equity/daily. No API, upload,
git pull, git push, or global ExecutionPolicy changes are performed.
CUDA wheel source (PyTorch 2.8.0, Linux and Windows, CUDA 12.6):
https://pytorch.org/get-started/previous-versions/#v280
#>
[CmdletBinding()]
param(
    [string]$Workspace = (Join-Path $env:USERPROFILE 'Desktop\QuantModel'),
    [string]$PythonPath = '',
    [ValidateRange(0, 100000)][int]$MaxStocks = 0,
    [ValidateRange(1, 1000)][int]$Epochs = 20,
    [ValidateSet(32, 64, 128)][int]$Hidden = 128,
    [ValidateRange(0, 65536)][int]$BatchSize = 0,
    [ValidateRange(0, 512)][int]$CpuThreads = 0,
    [ValidateSet('auto', 'cuda', 'cpu')][string]$Device = 'auto',
    [ValidateRange(1024, 65535)][int]$Port = 8768,
    [switch]$Tune = $true,
    [switch]$InstallCudaTorch,
    [switch]$ExitAfterTraining
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:LogFile = $null
$RepoRoot = Split-Path -Parent $PSScriptRoot

function Write-SafeLog {
    param([string]$Message)
    # Do not print environment variables; scrub common credential-bearing output.
    $safe = $Message -replace '(?i)(https?://)[^\s/@]+:[^\s/@]+@', '$1[REDACTED]@'
    $safe = $safe -replace '(?i)([?&](?:token|api_key|apikey|access_token|secret|password)=)[^&\s]+', '$1[REDACTED]'
    $safe = $safe -replace '(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+', '$1[REDACTED]'
    Write-Host $safe
    if ($script:LogFile) {
        Add-Content -LiteralPath $script:LogFile -Value $safe -Encoding UTF8
    }
}

function Invoke-Checked {
    param([string]$Executable, [string[]]$Arguments, [string]$Label)
    Write-SafeLog $Label
    $savedPreference = $ErrorActionPreference
    try {
        # Native stderr is an ErrorRecord in Windows PowerShell 5.1. The native
        # exit code, checked below, determines success rather than stderr alone.
        $ErrorActionPreference = 'Continue'
        & $Executable @Arguments 2>&1 | ForEach-Object { Write-SafeLog ([string]$_) }
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $savedPreference
    }
    if ($code -ne 0) {
        throw "$Label failed (exit code $code). See the local log."
    }
}

function Find-BootstrapPython {
    $candidates = @()
    if ($PythonPath) {
        $candidates += @{ Exe = $PythonPath; Prefix = @() }
    }
    else {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            $candidates += @{ Exe = 'py'; Prefix = @('-3.12') }
            $candidates += @{ Exe = 'py'; Prefix = @('-3') }
        }
        if (Get-Command python -ErrorAction SilentlyContinue) {
            $candidates += @{ Exe = 'python'; Prefix = @() }
        }
    }
    # Single quotes inside Python avoid legacy PowerShell native-argument
    # handling stripping nested double quotes from a python -c argument.
    $probe = "import sys,platform;sys.exit(0 if sys.platform=='win32' and (3,10)<=sys.version_info[:2]<(3,14) and platform.machine().lower() in ('amd64','x86_64') else 2)"
    foreach ($candidate in $candidates) {
        $savedPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            $arguments = @($candidate.Prefix) + @('-c', $probe)
            & $candidate.Exe @arguments 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { return $candidate }
        }
        catch { }
        finally { $ErrorActionPreference = $savedPreference }
    }
    throw 'A 64-bit Windows Python 3.10-3.13 is required. Python 3.12 is recommended. Use -PythonPath C:\path\python.exe if needed.'
}

try {
    if ($env:OS -ne 'Windows_NT') {
        throw 'This launcher is for Windows. It does not connect to another computer.'
    }
    $Requirements = Join-Path $RepoRoot 'requirements.txt'
    $History = Join-Path $RepoRoot 'data\equity\daily'
    $EntryPoint = Join-Path $RepoRoot 'pipeline\local_training.py'
    if (-not (Test-Path -LiteralPath $Requirements -PathType Leaf) -or
        -not (Test-Path -LiteralPath $EntryPoint -PathType Leaf)) {
        throw 'The repository is incomplete. Keep this script in the cloned repository scripts directory.'
    }
    $archives = @(Get-ChildItem -LiteralPath $History -Filter '*.json.gz' -File -ErrorAction SilentlyContinue)
    if ($archives.Count -eq 0) {
        throw 'No cached daily archives were found in data\equity\daily. This launcher does not download market data or request a token.'
    }

    # Resolve relative paths once, before changing to the repository directory.
    $Workspace = [System.IO.Path]::GetFullPath($Workspace)
    New-Item -ItemType Directory -Path $Workspace -Force | Out-Null
    $RunId = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + ([Guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Path (Join-Path $Workspace 'models') -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $Workspace 'logs') -Force | Out-Null
    $ModelRun = Join-Path (Join-Path $Workspace 'models') $RunId
    $LogRun = Join-Path (Join-Path $Workspace 'logs') $RunId
    New-Item -ItemType Directory -Path $ModelRun | Out-Null
    New-Item -ItemType Directory -Path $LogRun | Out-Null
    $script:LogFile = Join-Path $LogRun 'training.log'
    Write-SafeLog ('Run: ' + $RunId)
    Write-SafeLog ('Repository: ' + $RepoRoot)
    Write-SafeLog ('Cached archive files: ' + $archives.Count)
    Write-SafeLog ('Local results: ' + $ModelRun)
    Write-SafeLog ('Local log: ' + $script:LogFile)
    Write-SafeLog 'Training and tuning remain local. Nothing will be uploaded.'

    $Venv = Join-Path $Workspace '.venv-a-share-radar'
    $VenvMarker = Join-Path $Venv 'quantmodel-launcher.txt'
    $VenvPython = Join-Path $Venv 'Scripts\python.exe'
    if (Test-Path -LiteralPath $Venv) {
        if (-not (Test-Path -LiteralPath $VenvMarker -PathType Leaf) -or
            -not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
            throw 'The dedicated venv path already exists but is not a valid environment created by this launcher. Choose another -Workspace; existing files were not changed.'
        }
    }
    else {
        $bootstrap = Find-BootstrapPython
        Invoke-Checked -Executable $bootstrap.Exe -Arguments (@($bootstrap.Prefix) + @('-m', 'venv', $Venv)) -Label 'Creating dedicated Python environment'
        Set-Content -LiteralPath $VenvMarker -Value 'a-share-radar local training environment v1' -Encoding ASCII
    }
    Invoke-Checked -Executable $VenvPython -Arguments @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', '--progress-bar', 'off', '-r', $Requirements) -Label 'Installing repository base dependencies in the dedicated venv'

    if ($InstallCudaTorch) {
        Write-SafeLog 'Explicit CUDA installation requested: official PyTorch 2.8.0 / CUDA 12.6 wheel. A compatible NVIDIA GPU and driver are required.'
        Write-SafeLog 'Source: https://pytorch.org/get-started/previous-versions/#v280'
        Invoke-Checked -Executable $VenvPython -Arguments @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', '--progress-bar', 'off', '--upgrade', '--force-reinstall', 'torch==2.8.0', '--index-url', 'https://download.pytorch.org/whl/cu126') -Label 'Installing explicitly requested CUDA PyTorch wheel'
    }

    $preflight = @'
import importlib.util, json, platform, sys
print("Python: " + sys.version.split()[0] + " | " + platform.machine())
print("Interpreter: " + sys.executable)
if importlib.util.find_spec("torch") is None:
    print("PREFLIGHT_ERROR: PyTorch is not installed in this dedicated environment.")
    sys.exit(31)
import torch
print("PyTorch: " + torch.__version__)
print("Built CUDA runtime: " + str(torch.version.cuda))
available = torch.cuda.is_available()
print("CUDA available: " + str(available))
if available:
    for i in range(torch.cuda.device_count()):
        p = torch.cuda.get_device_properties(i)
        print("GPU %d: %s | %.2f GiB" % (i, p.name, p.total_memory / 1024**3))
if sys.argv[1] == "cuda" and not available:
    print("PREFLIGHT_ERROR: CUDA was requested but no usable CUDA device is available. Training was not started.")
    sys.exit(32)
selected = "cuda" if sys.argv[1] == "auto" and available else ("cpu" if sys.argv[1] == "auto" else sys.argv[1])
print("Selected training device: " + selected.upper())
if sys.argv[1] == "auto" and not available:
    print("AUTO MODE: No usable CUDA device detected; this run will use CPU, not GPU.")
if selected == "cuda":
    x = torch.ones((2, 2), device="cuda")
    value = (x @ x).sum().item()
    torch.cuda.synchronize()
    print("CUDA computation preflight: passed")
'@
    $PreflightPath = Join-Path $LogRun 'preflight.py'
    [System.IO.File]::WriteAllText($PreflightPath, $preflight, [System.Text.Encoding]::ASCII)
    try {
        Invoke-Checked -Executable $VenvPython -Arguments @($PreflightPath, $Device) -Label 'Checking Python, PyTorch and the actual local device'
    }
    catch {
        Write-SafeLog 'If PyTorch is missing, rerun this script with -InstallCudaTorch -Device cuda for the pinned NVIDIA CUDA build.'
        Write-SafeLog ('For CPU only, install explicitly: & "' + $VenvPython + '" -m pip install torch==2.8.0 --index-url https://download.pytorch.org/whl/cpu')
        Write-SafeLog 'For another GPU or driver combination, follow the official PyTorch installation instructions. No silent CUDA-to-CPU fallback is made when -Device cuda is used.'
        throw
    }

    $trainingArguments = @('-u', '-m', 'pipeline.local_training',
        '--history-dir', $History, '--output-dir', $ModelRun,
        '--device', $Device, '--max-stocks', [string]$MaxStocks,
        '--epochs', [string]$Epochs, '--hidden', [string]$Hidden,
        '--batch-size', [string]$BatchSize, '--cpu-threads', [string]$CpuThreads,
        '--port', [string]$Port)
    if ($Tune) { $trainingArguments += '--tune' }
    if ($ExitAfterTraining) { $trainingArguments += '--exit-after-training' }
    Write-SafeLog ('Local progress dashboard: http://127.0.0.1:' + $Port + '/')
    Write-SafeLog 'MaxStocks=0 selects all cached stocks. BatchSize=0 and CpuThreads=0 let the trainer inspect available resources.'
    Write-SafeLog 'The monitor stays open after training until Ctrl+C unless -ExitAfterTraining is supplied.'
    Push-Location -LiteralPath $RepoRoot
    try {
        Invoke-Checked -Executable $VenvPython -Arguments $trainingArguments -Label 'Starting local training, validation and progress monitoring'
    }
    finally { Pop-Location }
    Write-SafeLog ('Finished. Local artifacts remain at: ' + $ModelRun)
}
catch {
    Write-SafeLog ('STOPPED: ' + $_.Exception.Message)
    exit 1
}
