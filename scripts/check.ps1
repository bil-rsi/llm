# Runs the checks defined in C:\llm\CONSTRAINTS.md. Exit code 1 = a blocking check failed.
# Usage: check.ps1 -Stage fast|task|full
#   fast  lint errors in changed scripts + secret scan of uncommitted and untracked files (seconds)
#   task  fast + secret scan of branch commits + lint warning ratchet + Pester unit tests + retrieval eval vs baseline (< 60 s)
#   full  task + eval.ps1 -Category facts-local against the running server            (minutes, needs start.ps1)
param([ValidateSet('fast', 'task', 'full')][string]$Stage = 'task')
$Root = Split-Path $PSScriptRoot -Parent
$LintWarningFloor = 9          # non-Write-Host warnings in scripts\ on 2026-09-17; may go down, never up
$Gitleaks = "$env:LOCALAPPDATA\gitleaks\gitleaks.exe"
$failed = New-Object System.Collections.ArrayList

function Step([string]$Name, [scriptblock]$Body) {
    Write-Host "== $Name" -ForegroundColor Cyan
    $sw = [Diagnostics.Stopwatch]::StartNew()
    try { $ok = & $Body } catch { Write-Host $_ -ForegroundColor Red; $ok = $false }
    Write-Host ("   {0} ({1:N1}s)" -f $(if ($ok) { 'pass' } else { 'FAIL' }), $sw.Elapsed.TotalSeconds) -ForegroundColor $(if ($ok) { 'Green' } else { 'Red' })
    if (-not $ok) { [void]$failed.Add($Name) }
}

# Changed = modified/added (not deleted) tracked files plus untracked, non-ignored files. Only server logs, model
# weights and llama.cpp binaries are skipped; eval outputs under logs\ are scanned because they echo document text.
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$Excluded = @(':(exclude)logs/server*', ':(exclude)models/*.gguf', ':(exclude)llama.cpp')
function Get-GitPath([string[]]$GitArgs) { @(((git -c core.quotepath=off -C $Root @GitArgs -z -- . $Excluded) -join "`n") -split "`0" | Where-Object { $_ }) }
$names = @(Get-GitPath @('diff', 'HEAD', '--name-only', '--diff-filter=d')) + @(Get-GitPath @('ls-files', '--others', '--exclude-standard')) | Sort-Object -Unique
$changed = @($names | Where-Object { Test-Path -LiteralPath (Join-Path $Root $_) -PathType Leaf })
$unresolved = @($names | Where-Object { -not (Test-Path -LiteralPath (Join-Path $Root $_)) })

function Show-Lint($Results) { $Results | ForEach-Object { Write-Host ("   {0}:{1} {2} {3}" -f $_.ScriptName, $_.Line, $_.Severity, $_.RuleName) } }

Step 'lint changed scripts (PSScriptAnalyzer errors)' {
    $files = @($changed | Where-Object { $_ -match '\.(ps1|psm1|psd1)$' })
    if (-not $files.Count) { Write-Host '   no changed scripts'; return $true }
    $r = @($files | ForEach-Object { Invoke-ScriptAnalyzer -Path "$Root\$_" -Settings "$Root\PSScriptAnalyzerSettings.psd1" })
    Show-Lint $r
    Write-Host "   files=$($files.Count) errors=$(@($r | Where-Object Severity -eq 'Error').Count) warnings=$(@($r | Where-Object Severity -eq 'Warning').Count)"
    @($r | Where-Object Severity -eq 'Error').Count -eq 0
}

Step 'secrets (gitleaks, uncommitted + untracked)' {
    if (-not (Test-Path $Gitleaks)) { throw "gitleaks not found at $Gitleaks" }
    if ($unresolved.Count) { Write-Host "   could not resolve $($unresolved.Count) changed path(s), refusing to pass: $($unresolved -join ', ')"; return $false }
    if (-not $changed.Count) { Write-Host '   no uncommitted changes'; return $true }
    $text = @($changed | ForEach-Object { [IO.File]::ReadAllText((Join-Path $Root $_)) })
    ($text -join "`n") | & $Gitleaks stdin --redact --no-banner --log-level warn
    Write-Host "   scanned $($changed.Count) files"
    $LASTEXITCODE -eq 0
}

if ($Stage -in 'task', 'full') {
    Step 'secrets in branch commits (gitleaks, master..HEAD)' {
        $branch = git -C $Root rev-parse --abbrev-ref HEAD
        if ($branch -eq 'master' -or -not (git -C $Root rev-parse --verify --quiet master)) { Write-Host '   on master or no master branch'; return $true }
        & $Gitleaks git $Root --log-opts="master..HEAD" --redact --no-banner --log-level warn
        $LASTEXITCODE -eq 0
    }
    Step 'lint warning ratchet (all scripts)' {
        $r = @(Invoke-ScriptAnalyzer -Path "$Root\scripts" -Recurse -Settings "$Root\PSScriptAnalyzerSettings.psd1")
        $warnings = @($r | Where-Object Severity -eq 'Warning')
        Show-Lint $r
        Write-Host "   warnings=$($warnings.Count) (floor $LintWarningFloor)"
        @($r | Where-Object Severity -eq 'Error').Count -eq 0 -and $warnings.Count -le $LintWarningFloor
    }
    Step 'unit tests (Pester)' {
        $res = Invoke-Pester -Path "$Root\tests" -PassThru -Quiet
        Write-Host "   passed=$($res.PassedCount) failed=$($res.FailedCount) skipped=$($res.SkippedCount) pending=$($res.PendingCount)"
        $res.FailedCount -eq 0 -and $res.SkippedCount -eq 0 -and $res.PendingCount -eq 0 -and $res.TotalCount -gt 0
    }
    Step 'retrieval quality (rag-eval vs baseline)' {
        & powershell -NoProfile -ExecutionPolicy Bypass -File "$PSScriptRoot\rag-eval.ps1" -Tag check -Baseline "$Root\tests\fixtures\rag\baseline.json" | Out-Host
        $LASTEXITCODE -eq 0
    }
}

if ($Stage -eq 'full') {
    # Measured, not enforced: eval.ps1 only exits non-zero when the server is down. Compare runs with eval.ps1 -Compare.
    Step 'end-to-end eval (eval.ps1 facts-local, needs server)' {
        & powershell -NoProfile -ExecutionPolicy Bypass -File "$PSScriptRoot\eval.ps1" -Category facts-local -Tag check | Out-Host
        $LASTEXITCODE -eq 0
    }
}

if ($failed.Count) { Write-Host "BLOCKED: $($failed -join ', ')" -ForegroundColor Red; exit 1 }
Write-Host "all $Stage checks passed" -ForegroundColor Green
