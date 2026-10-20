# Inicia TRINITY CORE em modo de desenvolvimento
Set-Location "$PSScriptRoot\..\packages\core"
Write-Host "Starting TRINITY CORE on http://localhost:8000" -ForegroundColor Cyan
Write-Host "API Docs: http://localhost:8000/docs" -ForegroundColor Cyan
uv run uvicorn src.main:app --reload --port 8000 --host 0.0.0.0
