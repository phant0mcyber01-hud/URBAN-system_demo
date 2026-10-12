# Starts the platform server; UI at http://localhost:8080
Set-Location (Split-Path $PSScriptRoot -Parent)
.\.venv\Scripts\python -m urban
