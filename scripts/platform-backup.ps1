# On-demand database backup (pg_dump custom format) into platform\backups, using the running backup service.
# Usage: powershell -ExecutionPolicy Bypass -File C:\llm-platform\scripts\platform-backup.ps1
$ErrorActionPreference = 'Stop'
$plat = Join-Path (Split-Path $PSScriptRoot -Parent) 'platform'
# docker writes progress to stderr; PowerShell 5.1 would turn that into terminating errors under 'Stop'.
function Invoke-Docker { $ErrorActionPreference = 'Continue'; & docker @args 2>&1 | ForEach-Object { "$_" }; $global:DockerExit = $LASTEXITCODE }
Push-Location $plat
try { Invoke-Docker compose exec -T backup /bin/sh /opt/backup/backup.sh now | Out-Host; if ($DockerExit) { throw 'backup failed (is the platform running? scripts\platform-up.ps1)' } } finally { Pop-Location }
Get-ChildItem "$plat\backups\*.dump" | Sort-Object LastWriteTime -Descending | Select-Object -First 3 Name, @{ n = 'MB'; e = { [math]::Round($_.Length / 1MB, 2) } }, LastWriteTime | Format-Table -AutoSize
