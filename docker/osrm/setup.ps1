<#
.SYNOPSIS
  Prepare the OSRM data for RouteOpt on Windows (Docker Desktop required): the road
  network within 250 miles of the hub.

.DESCRIPTION
  Downloads the Geofabrik extracts that the coverage circle (coverage.geojson) touches
  (California, Nevada, Arizona) into .\data, builds the small routeopt-osmium helper
  image, filters/merges/clips the
  extracts to the circle (osmium\prepare.sh) and runs the OSRM MLD pipeline
  (osrm-extract -> osrm-partition -> osrm-customize) inside the OSRM container.
  Re-run to refresh the map.  Then start the server with:  docker compose up -d

.EXAMPLE
  .\setup.ps1
  .\setup.ps1 -ForceDownload
  .\setup.ps1 -IncludeMexico
  .\setup.ps1 -Regions us/california,us/nevada,us/arizona
#>
[CmdletBinding()]
param(
    [string]$DataDir = (Join-Path $PSScriptRoot "data"),
    # Geofabrik regions the 250-mile circle around the hub (Rialto, CA) touches (Utah's
    # nearest corner is 273 miles away).  If the hub or the radius changes (make_coverage.py),
    # check this list against the new circle: 500 miles would add us/utah and us/new-mexico.
    [string[]]$Regions = @("us/california", "us/nevada", "us/arizona"),
    [switch]$IncludeMexico,
    [string]$Geofabrik = "https://download.geofabrik.de/north-america",
    [string]$Name = "routeopt-coverage",   # must match docker-compose.yml
    [string]$OsrmImage = "ghcr.io/project-osrm/osrm-backend:v5.27.1",
    [string]$OsmiumImage = "routeopt-osmium",   # built locally from .\osmium
    [switch]$ForceDownload
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker Desktop is not installed or not on PATH. Install it from https://www.docker.com/products/docker-desktop/ and start it."
}

$poly = Join-Path $PSScriptRoot "coverage.geojson"
if (-not (Test-Path $poly)) {
    throw "Missing $poly - run:  python docker\osrm\make_coverage.py"
}

New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
Copy-Item $poly (Join-Path $DataDir "coverage.geojson") -Force

if ($IncludeMexico) { $Regions += "mexico" }

$inputs = @()
foreach ($region in $Regions) {
    $base = ($region -split "/")[-1] + "-latest.osm.pbf"
    $file = Join-Path $DataDir $base
    $url = "$Geofabrik/$region-latest.osm.pbf"
    if ((Test-Path $file) -and -not $ForceDownload) {
        Write-Host "Using existing $file (use -ForceDownload to re-download)"
    } else {
        Write-Host "Downloading $url ..."
        if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
            & curl.exe -L --fail --progress-bar -o $file $url
            if ($LASTEXITCODE -ne 0) { throw "download failed: $url" }
        } else {
            $ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is very slow with the progress bar
            Invoke-WebRequest -Uri $url -OutFile $file -UseBasicParsing
            $ProgressPreference = "Continue"
        }
    }
    $inputs += "/data/$base"
}

# Docker needs a path it can mount; on Windows use the absolute path with forward slashes.
$mount = (Resolve-Path $DataDir).Path -replace '\\', '/'

function Invoke-InContainer {
    param([string]$Image, [string[]]$ContainerArgs)
    & docker run --rm -t -v "${mount}:/data" $Image @ContainerArgs
    if ($LASTEXITCODE -ne 0) { throw "docker run $Image failed: $($ContainerArgs -join ' ')" }
}

Write-Host "Building the osmium helper image ($OsmiumImage) ..."
& docker build -q -t $OsmiumImage (Join-Path $PSScriptRoot "osmium") | Out-Null
if ($LASTEXITCODE -ne 0) { throw "docker build of $OsmiumImage failed" }

Write-Host "Filtering, merging and clipping the extracts to coverage.geojson ..."
Invoke-InContainer $OsmiumImage (@("routeopt-prepare", $Name, "/data/coverage.geojson") + $inputs)

Write-Host "osrm-extract (car profile) ..."
Invoke-InContainer $OsrmImage @("osrm-extract", "-p", "/opt/car.lua", "/data/$Name.osm.pbf")
Write-Host "osrm-partition ..."
Invoke-InContainer $OsrmImage @("osrm-partition", "/data/$Name.osrm")
Write-Host "osrm-customize ..."
Invoke-InContainer $OsrmImage @("osrm-customize", "/data/$Name.osrm")

Write-Host ""
Write-Host "Done. Start the server with:  docker compose -f `"$PSScriptRoot\docker-compose.yml`" up -d"
Write-Host "Health check (hub):           Invoke-RestMethod 'http://localhost:5000/nearest/v1/driving/-117.4319,34.1511'"
Write-Host "Coverage check (hub -> LV):   Invoke-RestMethod 'http://localhost:5000/route/v1/driving/-117.4319,34.1511;-115.1398,36.1699?overview=false'"
