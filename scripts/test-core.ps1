# Executa os testes do TRINITY CORE
Set-Location "$PSScriptRoot\..\packages\core"
Write-Host "Running TRINITY CORE tests..." -ForegroundColor Cyan
uv run pytest tests/ -v
