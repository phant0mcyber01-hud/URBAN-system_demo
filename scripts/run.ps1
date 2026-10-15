# Starts the real system: the city from data/city, real URBAN Edge units. UI at http://localhost:8080
Set-Location (Split-Path $PSScriptRoot -Parent)
.\.venv\Scripts\python -m urban
