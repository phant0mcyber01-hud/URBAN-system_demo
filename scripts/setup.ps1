# One-time setup: virtual environment, dependencies, UI build, data pipeline.
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
.\.venv\Scripts\python -m urban.pipeline
Write-Host "Done. Start with scripts\run.ps1"
