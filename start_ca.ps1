<#
.SYNOPSIS
  CA LFLT pilot: start everything with one command.

  1. Optionally update the code (git pull) when the folder is a git clone.
  2. Start the CA OSRM container (routeopt-osrm-ca, port 5001) and wait until it answers.
  3. Stop any RouteOpt app left running from an earlier session.
  4. Start the app and open the browser.

.EXAMPLE
  .\start_ca.ps1               # http://localhost:8520
  .\start_ca.ps1 -Update       # git pull first
  .\start_ca.ps1 -Port 8530
#>
[CmdletBinding()]
param(
    [int]$Port = 8520,
    [string]$OsrmContainer = "routeopt-osrm-ca",
    [string]$OsrmUrl = "http://localhost:5001",
    [switch]$Update
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if ($Update) {
    if (Get-Command git -ErrorAction SilentlyContinue) {
        Write-Host "Updating the code (git pull) ..."
        & git pull --ff-only
        if ($LASTEXITCODE -ne 0) { Write-Warning "git pull failed - starting with the current code." }
    } else {
        Write-Warning "git is not installed - download the latest ZIP instead (see README)."
    }
}

# ---- OSRM ------------------------------------------------------------------------------
# Docker writes its errors to stderr; with ErrorActionPreference = Stop, Windows PowerShell
# would turn that into a fatal error, so docker is called with Continue here.
function Invoke-Docker([string[]]$DockerArgs) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { $null = & docker @DockerArgs 2>&1; return $LASTEXITCODE -eq 0 }
    catch { return $false }
    finally { $ErrorActionPreference = $prev }
}
if (Get-Command docker -ErrorAction SilentlyContinue) {
    if (-not (Invoke-Docker @("info"))) {
        Write-Warning "Docker Desktop is not running: start it, or runs will use straight-line estimates."
    } else {
        # the CA container (port 5001), else the one docker\osrm\docker-compose.yml makes (5000)
        $candidates = @(@($OsrmContainer, $OsrmUrl), @("routeopt-osrm", "http://localhost:5000"))
        $started = $null
        foreach ($c in $candidates) {
            if (Invoke-Docker @("start", $c[0])) { $started = $c; break }
        }
        if (-not $started) {
            Write-Warning ("No OSRM container found ($OsrmContainer or routeopt-osrm). Build it once: " +
                "cd docker\osrm; .\setup.ps1; docker compose up -d  - until then runs use straight-line estimates.")
        } else {
            $name, $url = $started
            Write-Host "Starting OSRM ($name) ..."
            $ok = $false
            for ($i = 0; $i -lt 30 -and -not $ok; $i++) {
                try {
                    $r = Invoke-RestMethod "$url/nearest/v1/driving/-117.4319,34.1511" -TimeoutSec 2
                    $ok = $r.code -eq "Ok"
                } catch { Start-Sleep -Seconds 2 }
            }
            if ($ok) {
                Write-Host "OSRM is up at $url"
                $env:OSRM_URL = $url  # the app uses this container
            } else { Write-Warning "OSRM did not answer at $url - check the container in Docker Desktop." }
        }
    }
} else {
    Write-Warning "Docker is not installed - runs will use straight-line estimates."
}

# ---- stop an app left running from an earlier session -----------------------------------
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='streamlit.exe'" |
    Where-Object { $_.CommandLine -like "*streamlit*" -and $_.CommandLine -like "*Home.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# ---- app --------------------------------------------------------------------------------
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating .venv and installing the app (first run only) ..."
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { & py -3 -m venv .venv } else { & python -m venv .venv }
    & .venv\Scripts\python.exe -m pip install --quiet --upgrade pip
    & .venv\Scripts\pip.exe install --quiet -e .
}
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }
Write-Host "Starting RouteOpt on http://localhost:$Port ..."
Start-Process "http://localhost:$Port"
& .venv\Scripts\python.exe -m streamlit run app\Home.py --server.port $Port
