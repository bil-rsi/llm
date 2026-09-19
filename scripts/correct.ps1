# Records that an answer was wrong: appends the correction to rag\docs\corrections.md (re-indexed
# immediately, so it's retrieved by -Rag on|auto from now on) and, unless -NoEval, adds a regression
# check to eval\evalset.jsonl so eval.ps1 flags it if the same mistake ever comes back.
# Usage: correct.ps1 "question" "correct answer" [-Wrong "what it said"] [-NoEval]
param([Parameter(Mandatory, Position = 0)][string]$Question, [Parameter(Mandatory, Position = 1)][string]$Correct,
      [string]$Wrong = '', [switch]$NoEval)
Import-Module "$PSScriptRoot\rag.psm1" -DisableNameChecking -Force

$id = Add-Correction -Question $Question -Correct $Correct -Wrong $Wrong
$hit = @(Search-Rag $Question -K 1)
$ok = $hit.Count -and $hit[0].Chunk.id -eq $id
Write-Host "Saved correction ($id). Retrieval check: $(if ($ok) { 'OK, retrieves top-1' } else { 'WARNING: does not retrieve top-1 for this question' })" -ForegroundColor $(if ($ok) { 'Green' } else { 'Yellow' })

if (-not $NoEval) {
    $set = 'C:\llm\eval\evalset.jsonl'
    $existing = if (Test-Path $set) { Get-Content $set -Encoding UTF8 | Where-Object { $_.Trim() } | ForEach-Object { $_ | ConvertFrom-Json } } else { @() }
    $n = 1; foreach ($m in ($existing | ForEach-Object { if ($_.id -match '^corr-(\d+)$') { [int]$Matches[1] } })) { if ($m -ge $n) { $n = $m + 1 } }
    $keywords = @(Get-Tokens $Correct | Select-Object -Unique | Select-Object -First 8)   # capped so the check stays a fact check, not a wording match
    $item = [ordered]@{ id = "corr-$n"; category = 'corrections'; prompt = $Question; check = 'keywords'; expected = $keywords; rag = $true }
    Add-Content $set ($item | ConvertTo-Json -Compress) -Encoding UTF8
    Write-Host "Added eval regression check: corr-$n (run: eval.ps1 -Category corrections)"
}
