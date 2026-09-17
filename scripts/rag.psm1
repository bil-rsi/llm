# Lightweight BM25 retrieval over C:\llm\rag\docs (no extra model, no GPU memory).
#   Update-RagIndex            re-chunks changed files into C:\llm\rag\index.json
#   Search-Rag $q -K 4 -MaxTokens 1500
#   Format-RagPrompt $q $hits  -> user message with numbered sources

$RagRoot = 'C:\llm\rag'
$Stop = @('the','a','an','and','or','of','to','in','on','for','is','are','was','were','be','it','this','that','with','as','at','by','from',
          'what','how','do','does','i','you','my','your','we','can','yang','dan','di','ke','dari','ini','itu','untuk','dengan','adalah','apa','bagaimana')
$StopSet = New-Object 'System.Collections.Generic.HashSet[string]' -ArgumentList (,[string[]]$Stop)
$Exts = @('.md', '.txt', '.ps1', '.py', '.js', '.ts', '.json', '.yaml', '.yml', '.csv', '.html', '.log')
$ChunkerVersion = 2            # bump whenever Split-Doc output changes: forces a full re-chunk of every file
$script:Index = $null

function Get-Tokens([string]$Text) {
    foreach ($t in [regex]::Split($Text.ToLowerInvariant(), '[^\p{L}\p{N}_]+')) { if ($t.Length -gt 1 -and -not $StopSet.Contains($t)) { $t } }
}

function Split-Doc([string]$Rel, [string]$Text, [int]$Size = 1200, [int]$Overlap = 200) {
    $heading = ''; $buf = ''; $bufHeading = ''
    $chunks = New-Object System.Collections.ArrayList
    foreach ($para in [regex]::Split($Text, '(\r?\n){2,}')) {
        $para = $para.Trim(); if (-not $para) { continue }
        if ($para -match '^(#{1,6})\s+(.+)$') { $heading = $Matches[2].Trim() }
        while ($para.Length -gt $Size) {          # hard-split very long paragraphs
            [void]$chunks.Add(@{ heading = $heading; text = $para.Substring(0, $Size) }); $para = $para.Substring($Size - $Overlap)
        }
        if ($buf.Length + $para.Length + 2 -gt $Size -and $buf) {
            [void]$chunks.Add(@{ heading = $bufHeading; text = $buf })
            $buf = $buf.Substring([math]::Max(0, $buf.Length - $Overlap)); $bufHeading = $heading
        }
        if (-not $buf) { $bufHeading = $heading }
        $buf = if ($buf) { "$buf`n`n$para" } else { $para }
    }
    if ($buf) { [void]$chunks.Add(@{ heading = $bufHeading; text = $buf }) }
    $i = 0
    foreach ($c in $chunks) { [pscustomobject]@{ id = "$Rel#$i"; file = $Rel; heading = $c.heading; text = $c.text }; $i++ }
}

function Update-RagIndex {
    $docs = "$RagRoot\docs"; $idxFile = "$RagRoot\index.json"
    New-Item -ItemType Directory -Force $docs | Out-Null
    $old = @{}; $oldMtime = @{}; $rebuilt = $false
    if (Test-Path $idxFile) {
        $prev = Get-Content $idxFile -Raw | ConvertFrom-Json
        $prevVersion = if ($prev.chunker) { [int]$prev.chunker } else { 1 }
        if ($prevVersion -eq $ChunkerVersion) {
            foreach ($c in $prev.chunks) { if (-not $old[$c.file]) { $old[$c.file] = New-Object System.Collections.ArrayList }; [void]$old[$c.file].Add($c) }
            foreach ($p in $prev.files.PSObject.Properties) { $oldMtime[$p.Name] = $p.Value }
        } else { $rebuilt = $true }
    }
    $chunks = New-Object System.Collections.ArrayList; $files = [ordered]@{}; $changed = 0
    foreach ($f in Get-ChildItem $docs -Recurse -File | Where-Object { $Exts -contains $_.Extension.ToLower() }) {
        $rel = $f.FullName.Substring($docs.Length + 1) -replace '\\', '/'
        $mt = $f.LastWriteTimeUtc.Ticks.ToString(); $files[$rel] = $mt
        if ($oldMtime[$rel] -eq $mt -and $old[$rel]) { $chunks.AddRange(@($old[$rel])) }
        else { $chunks.AddRange(@(Split-Doc $rel ([IO.File]::ReadAllText($f.FullName)))); $changed++ }
    }
    @{ chunker = $ChunkerVersion; files = $files; chunks = $chunks } | ConvertTo-Json -Depth 5 -Compress | Set-Content $idxFile -Encoding UTF8
    $script:Index = $null
    $note = if ($rebuilt) { " (chunker v$prevVersion -> v$ChunkerVersion, rebuilt all)" } else { '' }
    Write-Host "Indexed $($files.Count) files ($changed changed) into $($chunks.Count) chunks: $idxFile$note"
}

function Get-RagIndex {
    if ($script:Index) { return $script:Index }
    $idxFile = "$RagRoot\index.json"
    if (-not (Test-Path $idxFile)) { throw "No RAG index. Put files in $RagRoot\docs and run rag-index.ps1" }
    $raw = Get-Content $idxFile -Raw | ConvertFrom-Json
    $df = @{}; $items = New-Object System.Collections.ArrayList; $total = 0
    foreach ($c in @($raw.chunks)) {
        $tf = @{}; $n = 0
        foreach ($t in Get-Tokens "$($c.heading) $($c.file) $($c.text)") { $tf[$t] = 1 + [int]$tf[$t]; $n++ }
        foreach ($t in $tf.Keys) { $df[$t] = 1 + [int]$df[$t] }
        [void]$items.Add([pscustomobject]@{ Chunk = $c; Tf = $tf; Len = $n }); $total += $n
    }
    $script:Index = [pscustomobject]@{ Items = $items; Df = $df; N = $items.Count; AvgDl = [math]::Max(1, $total / [math]::Max(1, $items.Count)) }
    $script:Index
}

# BM25 (k1=1.2, b=0.75). Drops hits under MinRel x top score, stops at K hits or ~MaxTokens (4 chars/token).
function Search-Rag([string]$Query, [int]$K = 4, [int]$MaxTokens = 1500, [double]$MinRel = 0.25) {
    $idx = Get-RagIndex
    $terms = @(Get-Tokens $Query | Select-Object -Unique)
    $scored = foreach ($it in $idx.Items) {
        $s = 0.0
        foreach ($t in $terms) {
            $f = [int]$it.Tf[$t]; if ($f -eq 0) { continue }
            $n = [int]$idx.Df[$t]
            $idf = [math]::Log(1 + ($idx.N - $n + 0.5) / ($n + 0.5))
            $s += $idf * ($f * 2.2) / ($f + 1.2 * (0.25 + 0.75 * $it.Len / $idx.AvgDl))
        }
        if ($s -gt 0) { [pscustomobject]@{ Score = $s; Chunk = $it.Chunk } }
    }
    $scored = @($scored | Sort-Object Score -Descending)
    if (-not $scored.Count) { return @() }
    $top = $scored[0].Score; $budget = $MaxTokens * 4; $used = 0
    $out = New-Object System.Collections.ArrayList
    foreach ($h in $scored) {
        if ($out.Count -ge $K -or $h.Score -lt $MinRel * $top) { break }
        if ($used + $h.Chunk.text.Length -gt $budget -and $out.Count) { break }
        [void]$out.Add($h); $used += $h.Chunk.text.Length
    }
    @($out)
}

function Format-RagPrompt([string]$Question, $Hits) {
    $n = 0; $src = @('(no matching documents found)')
    if (@($Hits).Count) { $src = foreach ($h in $Hits) { $n++; $hd = if ($h.Chunk.heading) { " > $($h.Chunk.heading)" } else { '' }; "[$n] $($h.Chunk.file)$hd`n$($h.Chunk.text)" } }
    "Answer using ONLY the sources below and cite them as [n]. If the sources do not contain the answer, say `"Not in my documents`" and then, if useful, add general knowledge clearly labelled as such.`n<sources>`n$($src -join "`n`n")`n</sources>`n`nQuestion: $Question"
}

Export-ModuleMember -Function Update-RagIndex, Search-Rag, Format-RagPrompt, Get-Tokens
