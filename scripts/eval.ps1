# Accuracy + latency harness against the running server.
# Usage:
#   eval.ps1 [-Think auto|on|off] [-Reps 2] [-Category math] [-Limit 5] [-Tag note]   -> logs\eval\<stamp>-<profile>-<tag>.json
#   eval.ps1 -RouterOnly                     check think-router decisions against should_think (no generation)
#   eval.ps1 -Compare a.json b.json          per-category accuracy and median-latency diff (also -Compare a.json, b.json in-process)
# Item format (eval\evalset.jsonl): {id, category, prompt, check, expected, schema?, rag?, should_think?}
#   check: number | choice | keywords | unknown | json | regex
# Parameters are named-only: under powershell -File, b.json in "-Compare a.json b.json" is a separate argument, collected by $CompareRest.
[CmdletBinding(PositionalBinding = $false)]
param([ValidateSet('auto', 'on', 'off')][string]$Think = 'auto', [int]$Reps = 1, [string]$Category = '', [int]$Limit = 0,
      [string]$Tag = '', [int]$Seed = 42, [string]$Set = 'C:\llm\eval\evalset.jsonl',
      [switch]$RouterOnly, [string[]]$Compare = @(), [Parameter(ValueFromRemainingArguments = $true)][string[]]$CompareRest = @())
if ($CompareRest.Count -and -not $Compare.Count) { Write-Host "Unexpected arguments: $($CompareRest -join ' '). All parameters are named."; exit 1 }
$Compare = @($Compare) + @($CompareRest)
if ($Compare.Count -and $Compare.Count -ne 2) { Write-Host "-Compare takes exactly two result files, got $($Compare.Count): $($Compare -join ' ')"; exit 1 }
. "$PSScriptRoot\lib.ps1"

function Get-Median([double[]]$v) { if (-not $v.Count) { return 0 }; $s = $v | Sort-Object; $n = $s.Count; if ($n % 2) { $s[[int][math]::Floor($n / 2)] } else { ($s[$n / 2 - 1] + $s[$n / 2]) / 2 } }

function Get-Summary($results) {
    foreach ($g in ($results | Group-Object category | Sort-Object Name)) {
        [pscustomobject]@{ category = $g.Name; n = $g.Count; acc = [math]::Round(100 * @($g.Group | Where-Object pass).Count / $g.Count, 1)
            med_ttft_s = [math]::Round((Get-Median @($g.Group | ForEach-Object { $_.prompt_ms / 1000 })), 1)
            med_wall_s = [math]::Round((Get-Median @($g.Group | ForEach-Object { $_.wall_ms / 1000 })), 1)
            med_tps = [math]::Round((Get-Median @($g.Group | ForEach-Object { $_.gen_tps })), 2) }
    }
}

if ($Compare.Count -eq 2) {
    $a = Get-Content $Compare[0] -Raw | ConvertFrom-Json; $b = Get-Content $Compare[1] -Raw | ConvertFrom-Json
    Write-Host "A: $($Compare[0]) [$($a.meta.profile) think=$($a.meta.think)]`nB: $($Compare[1]) [$($b.meta.profile) think=$($b.meta.think)]"
    $sa = @{}; foreach ($s in $a.summary) { $sa[$s.category] = $s }
    $b.summary | ForEach-Object { $x = $sa[$_.category]; [pscustomobject]@{ category = $_.category; acc_A = $x.acc; acc_B = $_.acc
        d_acc = $_.acc - $x.acc; wall_A = $x.med_wall_s; wall_B = $_.med_wall_s; tps_A = $x.med_tps; tps_B = $_.med_tps } } | Format-Table -AutoSize
    return
}

$items = Get-Content $Set -Encoding UTF8 | Where-Object { $_.Trim() } | ForEach-Object { $_ | ConvertFrom-Json }
if ($Category) { $items = @($items | Where-Object category -eq $Category) }
if ($Limit -gt 0) { $items = @($items | Select-Object -First $Limit) }
$prof = Get-ServerProfile

if ($RouterOnly) {
    $rows = foreach ($it in $items) {
        $d = Get-ThinkDecision $it.prompt ([int]$prof.ThinkThreshold) ([bool]$it.schema)
        [pscustomobject]@{ id = $it.id; should = [bool]$it.should_think; routed = $d.Think; score = $d.Score; ok = ([bool]$it.should_think -eq $d.Think); reasons = ($d.Reasons -join ',') }
    }
    $rows | Format-Table -AutoSize
    "Router agreement: {0}/{1} (threshold {2})" -f @($rows | Where-Object ok).Count, @($rows).Count, $prof.ThinkThreshold
    return
}
if (-not (Test-Server $prof.Port)) { Write-Host "Server not running. Start it with start.ps1."; exit 1 }

function Test-Item($it, $r) {
    $c = [string]$r.Content
    switch ($it.check) {
        'number'   { $m = [regex]::Matches(($c -replace '(?<=\d),(?=\d{3})', ''), '-?\d+(\.\d+)?'); if (-not $m.Count) { return $false }
                     $v = [double]$m[$m.Count - 1].Value; $e = [double]$it.expected; return [math]::Abs($v - $e) -le [math]::Max(1e-6, 1e-4 * [math]::Abs($e)) }
        'choice'   { $m = [regex]::Matches($c, '(?i)answer\s*(is)?\s*[:\uFF1A]?\s*\**\(?([A-E])\)?\b'); if ($m.Count) { return $m[$m.Count - 1].Groups[2].Value.ToUpper() -eq $it.expected }
                     return $c.Trim() -match "^\(?$($it.expected)\)?(\W|$)" }
        'keywords' { foreach ($k in @($it.expected)) { if ($c -notmatch [regex]::Escape($k)) { return $false } }; return $true }
        'unknown'  { return $c -match '(?i)not in my documents' }
        'regex'    { return $c.Trim() -match $it.expected }
        'json'     { if (-not $r.Json) { return $false }
                     foreach ($p in $it.expected.PSObject.Properties) { if ([string]$r.Json.($p.Name) -ne [string]$p.Value) { return $false } }; return $true }
    }
    $false
}

$results = New-Object System.Collections.ArrayList
$total = $items.Count * $Reps; $i = 0
for ($rep = 1; $rep -le $Reps; $rep++) {
    foreach ($it in $items) {
        $i++
        Write-Host ("[{0}/{1}] {2} ..." -f $i, $total, $it.id) -NoNewline
        $schema = if ($it.schema) { Join-Path 'C:\llm' $it.schema } else { '' }
        $rag = if ($it.rag) { 'on' } else { 'off' }
        try {
            $r = Invoke-Ask -Prompt $it.prompt -ThinkMode $Think -RagMode $rag -SchemaFile $schema -Seed ($Seed + $rep - 1) -ServerProfile $prof
            $pass = [bool](Test-Item $it $r); $err = ''
        } catch { $r = $null; $pass = $false; $err = $_.Exception.Message }
        Write-Host $(if ($pass) { ' PASS' } else { " FAIL $err" }) -ForegroundColor $(if ($pass) { 'Green' } else { 'Red' })
        [void]$results.Add([pscustomobject]@{ id = $it.id; category = $it.category; rep = $rep; pass = $pass; error = $err
            think = $r.Think; think_score = $r.ThinkScore; should_think = [bool]$it.should_think; json_valid = $r.JsonValid; retries = $r.Retries
            prompt_tokens = $r.PromptTokens; prompt_ms = $r.PromptMs; gen_tokens = $r.GenTokens; gen_tps = $r.GenTps
            draft_n = $r.DraftN; draft_accepted = $r.DraftAccepted; reasoning_chars = ([string]$r.Reasoning).Length; wall_ms = $r.WallMs
            sources = $r.Sources; answer = $r.Content })
    }
}

$summary = @(Get-Summary $results)
$all = [pscustomobject]@{ category = 'ALL'; n = $results.Count; acc = [math]::Round(100 * @($results | Where-Object pass).Count / [math]::Max(1, $results.Count), 1)
    med_ttft_s = [math]::Round((Get-Median @($results | ForEach-Object { $_.prompt_ms / 1000 })), 1)
    med_wall_s = [math]::Round((Get-Median @($results | ForEach-Object { $_.wall_ms / 1000 })), 1)
    med_tps = [math]::Round((Get-Median @($results | ForEach-Object { $_.gen_tps })), 2) }
$summary += $all
New-Item -ItemType Directory -Force C:\llm\logs\eval | Out-Null
$name = "{0}-{1}-{2}-think{3}{4}" -f (Get-Date -Format 'yyyyMMdd-HHmmss'), $prof.Profile, $prof.Model, $Think, $(if ($Tag) { "-$Tag" } else { '' })
$out = "C:\llm\logs\eval\$name.json"
@{ meta = @{ profile = $prof.Profile; model = $prof.Model; server = $prof; think = $Think; reps = $Reps; seed = $Seed; set = $Set; tag = $Tag
             total_wall_s = [math]::Round((($results | Measure-Object wall_ms -Sum).Sum) / 1000, 1) }
   summary = $summary; results = $results } | ConvertTo-Json -Depth 8 | Set-Content $out -Encoding UTF8
$summary | Format-Table -AutoSize
Write-Host "Saved: $out"
