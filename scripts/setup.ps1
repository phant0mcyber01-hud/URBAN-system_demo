# One-time setup: virtual environment, dependencies, UI build, the city and the demo twin.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Test-Path .venv)) { python -m venv .venv }
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e "backend[dev]"

Push-Location web
npm install
npm run build
Pop-Location

if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# the real city: map and junction passports from OpenStreetMap (area: BBOX in .env)
if (-not (Test-Path data\city\signals.json)) { .\.venv\Scripts\python -m urban.city create --name "Фергана, центр" }
# the demo twin for presentations: SUMO network, traffic and synthetic junctions
if (-not (Test-Path data\out\signals.json)) { .\.venv\Scripts\python -m urban.pipeline }
Write-Host "Done. The real system: scripts\run.ps1   The demo twin: scripts\demo.ps1"
