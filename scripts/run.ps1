[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$NoScheduler,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

function Test-PythonRuntime {
    param([string]$PythonPath)
    & $PythonPath -c "import importlib.util, sys; sys.exit(0 if all(importlib.util.find_spec(name) for name in ('fastapi', 'uvicorn', 'playwright', 'ytmusicapi', 'httpx')) else 1)"
    return $LASTEXITCODE -eq 0
}

$venv = Join-Path $projectRoot '.venv'
$venvPython = Join-Path $venv 'Scripts\python.exe'
$pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
if ((Test-Path -LiteralPath $venvPython) -and (Test-PythonRuntime -PythonPath $venvPython)) {
    $systemPython = $venvPython
} elseif ($pythonCommand -and $pythonCommand.Source) {
    $systemPython = $pythonCommand.Source
} else {
    $pythonCandidates = @(
        (Join-Path $env:LocalAppData 'Programs\Python\Python312\python.exe'),
        (Join-Path $env:LocalAppData 'Programs\Python\Python311\python.exe'),
        (Join-Path $env:ProgramFiles 'Python312\python.exe'),
        (Join-Path $env:ProgramFiles 'Python311\python.exe')
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
    if (-not $pythonCandidates -and (Test-Path -LiteralPath $venvPython)) {
        $pythonCandidates = @($venvPython)
    }
    if (-not $pythonCandidates) {
        throw 'Python 3 was not found. Install Python or add python.exe to PATH once, then rerun the launcher.'
    }
    $systemPython = $pythonCandidates | Select-Object -First 1
}
$python = $systemPython
$dependenciesReady = Test-PythonRuntime -PythonPath $systemPython

if ($Install -or -not $dependenciesReady) {
    if (-not (Test-Path -LiteralPath (Join-Path $venv 'Scripts\python.exe'))) {
        & $systemPython -m venv $venv
    }
    $python = Join-Path $venv 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        throw "The virtual environment could not be created at $venv"
    }
    $dependenciesReady = Test-PythonRuntime -PythonPath $python
}
if ($Install -or -not $dependenciesReady) {
    Write-Host "Installing runtime dependencies..."
    & $python -m pip install --disable-pip-version-check --no-input --default-timeout 30 --retries 1 --upgrade pip
    & $python -m pip install --disable-pip-version-check --no-input --default-timeout 30 --retries 1 -r (Join-Path $projectRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw "Runtime dependency installation failed with exit code $LASTEXITCODE" }
} elseif ($python -eq $systemPython) {
    Write-Host "Using the available Python runtime: $python"
}
if ($NoScheduler) { $env:YTMUSIC_RECOMMENDER_SCHEDULER_INTERVAL_SECONDS = '0' }

if (-not $NoBrowser) {
    $chromePath = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
    $approvedChrome = @(Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" | Where-Object {
        [string]::Equals([string]$_.ExecutablePath, $chromePath, [StringComparison]::OrdinalIgnoreCase)
    })
    if ($approvedChrome.Count -eq 0 -and (Test-Path -LiteralPath $chromePath)) {
        $chromeHook = Join-Path $env:USERPROFILE '.codex\hooks\Ensure-VisibleChromeProfile2.ps1'
        if (Test-Path -LiteralPath $chromeHook) {
            Write-Host 'Starting approved Chrome through the profile-preserving recovery hook...'
            & "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File $chromeHook -WaitSeconds 10
            if ($LASTEXITCODE -ne 0) { Write-Warning 'Approved Chrome recovery did not succeed; the app will report bridge-waiting state.' }
        } else {
            Write-Warning 'Approved Chrome recovery hook is missing; the app will report bridge-waiting state.'
        }
    } elseif ($approvedChrome.Count -gt 0) {
        Write-Host "Approved Chrome is already running; preserving its existing tabs and extension state."
    } else {
        Write-Warning "Approved Chrome was not found; the app will remain in bridge-waiting mode."
    }
}

Write-Host "Starting YouTube Music Personal Mix on http://127.0.0.1:8000 ..."
& $python -m app.main
