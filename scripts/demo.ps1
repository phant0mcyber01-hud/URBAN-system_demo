# Starts the demo twin for presentations: SUMO traffic, synthetic junctions, emulated units. http://localhost:8080
# It keeps its own keys and history (data/demo), apart from the real city.
Set-Location (Split-Path $PSScriptRoot -Parent)
.\.venv\Scripts\python -m urban --demo
