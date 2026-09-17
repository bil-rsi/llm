# Runs the checks defined in C:\llm\CONSTRAINTS.md. Exit code 1 = a blocking check failed.
# Usage: check.ps1 -Stage fast|task|full
#   fast  lint errors in changed scripts + secret scan of uncommitted and untracked files (seconds)
#   task  fast + lint warning ratchet + Pester unit tests + retrieval eval vs tests\fixtures\rag\baseline.json (< 60 s)
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

$Excluded = @(':(exclude)logs', ':(exclude)models', ':(exclude)llama.cpp')
$changed = @(git -C $Root diff HEAD --name-only -- . $Excluded) + @(git -C $Root ls-files --others --exclude-standard -- . $Excluded) |
    Where-Object { $_ -and (Test-Path "$Root\$_") } | Sort-Object -Unique

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
    if (-not $changed.Count) { Write-Host '   no uncommitted changes'; return $true }
    $text = @(git -C $Root diff HEAD -- . $Excluded) + @($changed | ForEach-Object { Get-Content "$Root\$_" -Raw })
    ($text -join "`n") | & $Gitleaks stdin --redact --no-banner --log-level warn
    Write-Host "   scanned $($changed.Count) files"
    $LASTEXITCODE -eq 0
}

if ($Stage -in 'task', 'full') {
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
