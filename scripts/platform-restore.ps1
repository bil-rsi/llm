# Restores a backup. Safe by default: restores into a NEW database (aiplatform_restore), verifies it (row counts + audit
# hash chain), and only replaces the live database when you pass -Swap AND confirm. A fresh backup of the live database
# is taken first, so the swap itself can be undone.
# Usage: platform-restore.ps1 -File platform\backups\aiplatform-YYYYMMDD-HHMMSS.dump [-Swap]
param([Parameter(Mandatory)][string]$File, [switch]$Swap)
$ErrorActionPreference = 'Stop'
$plat = Join-Path (Split-Path $PSScriptRoot -Parent) 'platform'
# docker writes progress to stderr; PowerShell 5.1 would turn that into terminating errors under 'Stop'.
function Invoke-Docker { $ErrorActionPreference = 'Continue'; & docker @args 2>&1 | ForEach-Object { "$_" }; $global:DockerExit = $LASTEXITCODE }
$dump = (Resolve-Path $File).Path
if ((Split-Path $dump -Parent) -ne (Resolve-Path "$plat\backups").Path) { throw "Backups must be in $plat\backups" }
$name = Split-Path $dump -Leaf
Push-Location $plat
try {
    $env:PGPASSWORD = (Get-Content "$plat\secrets\pg_superuser.txt" -Raw).Trim()
    function Psql([string]$Db, [string]$Sql) { $out = Invoke-Docker compose exec -T -e PGPASSWORD postgres psql -v ON_ERROR_STOP=1 -U postgres -d $Db -At -c $Sql; if ($DockerExit) { throw "psql failed: $Sql`n$out" }; $out }
    Psql postgres 'DROP DATABASE IF EXISTS aiplatform_restore' | Out-Null
    Psql postgres 'CREATE DATABASE aiplatform_restore OWNER aimem_owner' | Out-Null
    Psql aiplatform_restore 'CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pg_stat_statements; CREATE SCHEMA app AUTHORIZATION aimem_owner' | Out-Null
    Invoke-Docker compose cp $dump "postgres:/tmp/$name" | Out-Null
    $log = Invoke-Docker compose exec -T -e PGPASSWORD postgres pg_restore -U postgres -d aiplatform_restore --no-owner --role=aimem_owner --schema=app --exit-on-error "/tmp/$name"
    if ($DockerExit) { throw "pg_restore failed:`n$log" }
    Invoke-Docker compose exec -T postgres rm -f "/tmp/$name" | Out-Null
    $counts = Psql aiplatform_restore "SELECT (SELECT count(*) FROM app.long_term_memories)||' memories, '||(SELECT count(*) FROM app.conversations)||' conversations, audit chain '||CASE WHEN app.audit_verify() IS NULL THEN 'intact' ELSE 'BROKEN' END"
    Write-Host "Restored copy verified: $counts (database aiplatform_restore)"
    if (-not $Swap) { Write-Host 'Live database unchanged. Re-run with -Swap to replace it.'; return }
    $answer = Read-Host "Replace the LIVE database with this backup? A backup of the current data is taken first. Type YES"
    if ($answer -ne 'YES') { Write-Host 'Cancelled.'; return }
    & "$PSScriptRoot\platform-backup.ps1"
    Invoke-Docker compose stop backend backup | Out-Null
    Psql postgres "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='aiplatform' AND pid <> pg_backend_pid()" | Out-Null
    Psql postgres "ALTER DATABASE aiplatform RENAME TO aiplatform_before_restore_$(Get-Date -Format yyyyMMddHHmmss)" | Out-Null
    Psql postgres 'ALTER DATABASE aiplatform_restore RENAME TO aiplatform' | Out-Null
    Psql aiplatform "REVOKE ALL ON DATABASE aiplatform FROM PUBLIC; GRANT CONNECT ON DATABASE aiplatform TO aimem_app, aimem_backup; GRANT USAGE ON SCHEMA app TO aimem_app, aimem_backup; GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA app TO aimem_app; GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA app TO aimem_app; REVOKE UPDATE, DELETE, TRUNCATE ON app.audit_log FROM aimem_app" | Out-Null
    Invoke-Docker compose start backup backend | Out-Null
    Write-Host 'Swapped. The previous live database was kept as aiplatform_before_restore_* (drop it yourself when satisfied).'
} finally { Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue; Pop-Location }
