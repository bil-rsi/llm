# Platform quality gate: lint (ruff), types (mypy --strict), tests (pytest, 0 failed/0 skipped, >=1 test) against a throwaway
# Postgres, dependency audit (pip-audit), security lint (bandit), secret scan (gitleaks, if installed). Exit code != 0 on failure.
# Usage: powershell -ExecutionPolicy Bypass -File C:\llm-platform\scripts\platform-check.ps1 [-Only lint|types|tests|audit] [-Pytest "-k memory"]
param([ValidateSet('', 'lint', 'types', 'tests', 'audit')][string]$Only = '', [string]$Pytest = '')
$ErrorActionPreference = 'Stop'
$plat = Join-Path (Split-Path $PSScriptRoot -Parent) 'platform'
$failed = New-Object System.Collections.ArrayList
function Quiet([scriptblock]$Cmd) { $ErrorActionPreference = 'Continue'; & $Cmd 2>&1 | ForEach-Object { "$_" } | Out-Null; $LASTEXITCODE }
function New-Pw { -join ((1..24) | ForEach-Object { '{0:x2}' -f (Get-Random -Maximum 256) }) }
function Invoke-Dev([string]$Cmd) { $ErrorActionPreference = 'Continue'; & docker run --rm -v "${plat}\backend\src:/src/src:ro" -v "${plat}\backend\tests:/src/tests:ro" -v "${plat}\backend\pyproject.toml:/src/pyproject.toml:ro" -v "${plat}\tool-runner:/src/tool-runner:ro" -w /src aiplatform-backend-dev:latest sh -c $Cmd 2>&1 | ForEach-Object { "$_" } | Out-Host; return $LASTEXITCODE }
function Step([string]$Name, [scriptblock]$Body) { $t = [Diagnostics.Stopwatch]::StartNew(); $code = @(& $Body)[-1]; $t.Stop(); $ok = ($code -eq 0); Write-Host ("[{0}] {1} ({2:n1} s)" -f $(if ($ok) { 'PASS' } else { 'FAIL' }), $Name, $t.Elapsed.TotalSeconds) -ForegroundColor $(if ($ok) { 'Green' } else { 'Red' }); if (-not $ok) { [void]$failed.Add($Name) } }

& docker build -q --target dev -t aiplatform-backend-dev:latest "$plat\backend" | Out-Null
if ($LASTEXITCODE) { throw 'building the dev image failed' }
if (-not $Only -or $Only -eq 'lint')  { Step 'ruff (lint)' { Invoke-Dev 'ruff check src tests && ruff check --select E,F,W,I,S /src/tool-runner/runner.py' } }
if (-not $Only -or $Only -eq 'types') { Step 'mypy --strict (src)' { Invoke-Dev 'mypy src' } }
if (-not $Only -or $Only -eq 'audit') {
    Step 'pip-audit (dependencies)' { Invoke-Dev 'pip-audit --progress-spinner off --skip-editable -l 2>&1 | tail -5; exit ${PIPESTATUS:-0}' }
    Step 'bandit (security lint)' { Invoke-Dev 'bandit -q -r src -ll' }
    $gl = "$env:LOCALAPPDATA\gitleaks\gitleaks.exe"
    if (Test-Path $gl) { Step 'gitleaks (secrets in platform/)' { & $gl dir "$plat" --no-banner --redact -l warn --config "$plat\.gitleaks.toml" | Out-Null; $LASTEXITCODE } }
    else { Write-Host '[SKIP] gitleaks not installed (see CONSTRAINTS.md)' -ForegroundColor Yellow }
}
if (-not $Only -or $Only -eq 'tests') {
    $env:AIP_TEST_SUPER_PW = New-Pw; $env:AIP_TEST_OWNER_PW = New-Pw; $env:AIP_TEST_APP_PW = New-Pw; $env:AIP_TEST_BACKUP_PW = New-Pw
    Push-Location $plat
    try {
        [void](Quiet { docker compose -f compose.test.yaml down -v })   # never reuse a container from an earlier run:
        # its roles were created with that run's random passwords
        if (Quiet { docker compose -f compose.test.yaml up -d --wait testdb }) { throw 'test database did not start' }
        Step 'pytest (unit, db, api, security)' { $ErrorActionPreference = 'Continue'; & docker compose -f compose.test.yaml run --rm tests sh -c "pytest -q -p no:cacheprovider -rfEs $Pytest --junitxml=/tmp/junit.xml; a=`$?; python tests/_junit_gate.py /tmp/junit.xml; b=`$?; [ `$a -eq 0 ] && [ `$b -eq 0 ]" 2>&1 | ForEach-Object { "$_" } | Out-Host; $LASTEXITCODE }
    } finally { [void](Quiet { docker compose -f compose.test.yaml down -v }); Pop-Location }
}
if ($failed.Count) { Write-Host "FAILED: $($failed -join ', ')" -ForegroundColor Red; exit 1 }
Write-Host 'All platform checks passed.' -ForegroundColor Green
