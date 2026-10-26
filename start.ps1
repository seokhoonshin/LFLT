<#
.SYNOPSIS
  Start RouteOpt on Windows: OSRM in Docker (pre-built data in docker\osrm\data), then the
  Streamlit app.  The first run creates .venv and installs the app.

.EXAMPLE
  .\start.ps1                 # app on http://0.0.0.0:8501
  .\start.ps1 -Port 8080 -BindAddress 127.0.0.1
#>
[CmdletBinding()]
param(
    [string]$BindAddress = $(if ($env:ROUTEOPT_HOST) { $env:ROUTEOPT_HOST } else { "0.0.0.0" }),
    [int]$Port = $(if ($env:ROUTEOPT_PORT) { [int]$env:ROUTEOPT_PORT } else { 8501 })
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker Desktop is not installed or not on PATH."
}
& docker info *> $null
if ($LASTEXITCODE -ne 0) { throw "Docker is installed but its engine is not running. Start Docker Desktop and try again." }
if (-not (Get-ChildItem "docker\osrm\data\routeopt-coverage.osrm.*" -ErrorAction SilentlyContinue)) {
    throw "No OSRM dataset in docker\osrm\data - run docker\osrm\setup.ps1 first (see docker\osrm\README.md)."
}

Write-Host "Starting OSRM ..."
Push-Location docker\osrm
& docker compose up -d
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "docker compose up failed" }
Pop-Location

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    $py = Get-Command py -ErrorAction SilentlyContinue
    Write-Host "Creating .venv ..."
    if ($py) { & py -3 -m venv .venv } else { & python -m venv .venv }
    if ($LASTEXITCODE -ne 0) { throw "could not create .venv (Python 3.12+ required)" }
    & .venv\Scripts\python.exe -m pip install --quiet --upgrade pip
    & .venv\Scripts\pip.exe install --quiet -e .
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
}

# OSRM needs a few seconds to load after `up`; report, but never block the app on it.
foreach ($i in 1..6) {
    $status = & .venv\Scripts\routeopt.exe osrm-check 2>$null
    if ("$status" -match "healthy") { break }
    Start-Sleep -Seconds 5
}
& .venv\Scripts\routeopt.exe osrm-check

Write-Host "RouteOpt on http://${BindAddress}:$Port  (Ctrl-C stops the app; OSRM keeps running in Docker)"
& .venv\Scripts\streamlit.exe run app\Home.py --server.address $BindAddress --server.port $Port --server.headless true
