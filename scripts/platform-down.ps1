# Stops the platform containers. Data (the aiplatform_pgdata volume) is kept; nothing is deleted.
# Usage: powershell -ExecutionPolicy Bypass -File C:\llm-platform\scripts\platform-down.ps1
$ErrorActionPreference = 'Stop'
$plat = Join-Path (Split-Path $PSScriptRoot -Parent) 'platform'
# docker writes progress to stderr; PowerShell 5.1 would turn that into terminating errors under 'Stop'.
function Invoke-Docker { $ErrorActionPreference = 'Continue'; & docker @args 2>&1 | ForEach-Object { "$_" }; $global:DockerExit = $LASTEXITCODE }
Push-Location $plat
try { Invoke-Docker compose --profile search -f compose.yaml -f compose.mounts.yaml down | Out-Host; if ($DockerExit) { throw 'docker compose down failed' } } finally { Pop-Location }
Write-Host 'Stopped. Database volume aiplatform_pgdata kept.'
