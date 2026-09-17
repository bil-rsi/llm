# Retrieval-only eval (no LLM server): indexes tests\fixtures\rag\docs into a temp root and checks whether the
# chunk containing each query's "needle" is retrieved. Writes logs\rag-eval\<timestamp>[-tag].json.
# Usage: rag-eval.ps1 [-Tag note]                      measure
#        rag-eval.ps1 -Baseline logs\rag-eval\x.json   measure and exit 1 if any quality metric falls below the baseline
param([string]$Tag = '', [string]$Baseline = '', [string]$Fixture = "$PSScriptRoot\..\tests\fixtures\rag")
$ErrorActionPreference = 'Stop'
Import-Module "$PSScriptRoot\rag.psm1" -DisableNameChecking -Force
$rag = Get-Module rag

$root = Join-Path ([IO.Path]::GetTempPath()) "rag-eval-$PID"
if (Test-Path $root) { Remove-Item $root -Recurse -Force }
New-Item -ItemType Directory -Force "$root\docs" | Out-Null
Copy-Item "$Fixture\docs\*" "$root\docs" -Recurse
& $rag { param($r) $script:RagRoot = $r; $script:Index = $null } $root

try {
    $buildMs = (Measure-Command { Update-RagIndex 6>$null }).TotalMilliseconds
    $idx = Get-Content "$root\index.json" -Raw | ConvertFrom-Json
    $lens = @($idx.chunks | ForEach-Object { $_.text.Length })
    $loadMs = (Measure-Command { & $rag { $script:Index = $null; Get-RagIndex | Out-Null } }).TotalMilliseconds
    $queries = @(Get-Content "$Fixture\queries.jsonl" | Where-Object { $_.Trim() } | ForEach-Object { $_ | ConvertFrom-Json })

    # Rank = position of the first retrieved chunk whose text contains the needle (0 = not retrieved).
    function Get-Rank($Hits, [string]$Needle) { $i = 0; foreach ($h in @($Hits)) { $i++; if ($h.Chunk.text.Contains($Needle)) { return $i } }; 0 }
    $rows = foreach ($q in $queries) {
        [pscustomobject]@{
            id       = $q.id
            rank     = Get-Rank (Search-Rag $q.query -K 10 -MaxTokens 100000 -MinRel 0) $q.needle
            speed    = [bool](Get-Rank (Search-Rag $q.query -K 2 -MaxTokens 800) $q.needle)      # speed profile RAG settings
            accuracy = [bool](Get-Rank (Search-Rag $q.query -K 4 -MaxTokens 2000) $q.needle)     # accuracy profile RAG settings
        }
    }
    $n = $rows.Count
    $m = [ordered]@{
        hit1          = [math]::Round(@($rows | Where-Object { $_.rank -eq 1 }).Count / $n, 3)
        hit4          = [math]::Round(@($rows | Where-Object { $_.rank -ge 1 -and $_.rank -le 4 }).Count / $n, 3)
        mrr           = [math]::Round(($rows | ForEach-Object { if ($_.rank) { 1.0 / $_.rank } else { 0 } } | Measure-Object -Sum).Sum / $n, 3)
        speed_recall  = [math]::Round(@($rows | Where-Object speed).Count / $n, 3)
        acc_recall    = [math]::Round(@($rows | Where-Object accuracy).Count / $n, 3)
    }
    $result = [ordered]@{
        time = (Get-Date).ToString('s'); tag = $Tag; queries = $n; quality = $m
        index = [ordered]@{ chunks = $lens.Count; max_chars = ($lens | Measure-Object -Maximum).Maximum; avg_chars = [math]::Round(($lens | Measure-Object -Average).Average) }
        timing_ms = [ordered]@{ build = [math]::Round($buildMs); load = [math]::Round($loadMs) }
        items = $rows
    }
} finally { Remove-Item $root -Recurse -Force -ErrorAction SilentlyContinue }

$outDir = "$PSScriptRoot\..\logs\rag-eval"; New-Item -ItemType Directory -Force $outDir | Out-Null
$name = (Get-Date -Format 'yyyyMMdd-HHmmss') + $(if ($Tag) { "-$Tag" } else { '' })
$result | ConvertTo-Json -Depth 5 | Set-Content "$outDir\$name.json" -Encoding UTF8
Write-Host ("quality  " + (($m.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join '  '))
Write-Host ("index    chunks=$($result.index.chunks) max_chars=$($result.index.max_chars) avg_chars=$($result.index.avg_chars)  build=$($result.timing_ms.build)ms load=$($result.timing_ms.load)ms")
foreach ($r in $rows | Where-Object { $_.rank -ne 1 }) { Write-Host ("  miss@1  {0,-14} rank={1} speed={2} accuracy={3}" -f $r.id, $r.rank, $r.speed, $r.accuracy) -ForegroundColor Yellow }
Write-Host "saved $outDir\$name.json"

if ($Baseline) {
    $base = (Get-Content $Baseline -Raw | ConvertFrom-Json).quality; $fail = $false
    foreach ($k in $m.Keys) {
        if ($m[$k] -lt $base.$k) { Write-Host "REGRESSION $k $($base.$k) -> $($m[$k])" -ForegroundColor Red; $fail = $true }
    }
    if ($fail) { exit 1 } else { Write-Host "no quality regression vs $Baseline" -ForegroundColor Green }
}
