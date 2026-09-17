# Lightweight BM25 retrieval over C:\llm\rag\docs (no extra model, no GPU memory).
#   Update-RagIndex            re-chunks changed files into C:\llm\rag\index.json
#   Search-Rag $q -K 4 -MaxTokens 1500
#   Format-RagPrompt $q $hits  -> user message with numbered sources

$RagRoot = 'C:\llm\rag'
$Stop = @('the','a','an','and','or','of','to','in','on','for','is','are','was','were','be','it','this','that','with','as','at','by','from',
          'what','how','do','does','i','you','my','your','we','can','yang','dan','di','ke','dari','ini','itu','untuk','dengan','adalah','apa','bagaimana')
$StopSet = New-Object 'System.Collections.Generic.HashSet[string]' -ArgumentList (,[string[]]$Stop)
$Exts = @('.md', '.txt', '.ps1', '.py', '.js', '.ts', '.json', '.yaml', '.yml', '.csv', '.html', '.log')
$ChunkerVersion = 4           # bump whenever Split-Doc output changes: forces a full re-chunk of every file
$script:Index = $null

function Get-Tokens([string]$Text) {
    foreach ($t in [regex]::Split($Text.ToLowerInvariant(), '[^\p{L}\p{N}_]+')) { if ($t.Length -gt 1 -and -not $StopSet.Contains($t)) { $t } }
}

# Blocks = headings, blank-line-separated paragraphs and fenced code blocks, in document order, as
# @(kind, text, headingLabel) arrays (plain arrays: per-block objects and function calls dominated run time).
# Kind 'lines' (fences, tables, code files) is only ever cut at line ends; 'prose' at sentence or word ends.
# Headings and fences are only recognised in markdown; other files are plain paragraphs of $PlainKind.
function Get-DocBlock([string]$Text, [bool]$Markdown = $true, [string]$PlainKind = 'prose') {
    $blocks = New-Object System.Collections.ArrayList
    $para = New-Object System.Collections.ArrayList
    $fence = ''; $closeFence = ''
    foreach ($line in ($Text -split '\r?\n') + '') {          # trailing '' flushes the last paragraph
        $t = $line.Trim()
        if ($fence) {                                          # CommonMark: close on the same char, at least as long, nothing after
            [void]$para.Add($line)
            if ($t.StartsWith($fence) -and $t -match $closeFence) { [void]$blocks.Add(@('lines', ($para -join "`n").Trim(), '')); $para.Clear(); $fence = '' }
            continue
        }
        $isOpen = $Markdown -and ($t.StartsWith('```') -or $t.StartsWith('~~~')) -and $t -match '^(`{3,}|~{3,})'
        $isHeading = -not $isOpen -and $Markdown -and $line.StartsWith('#') -and $line -match '^#{1,6}\s+\S'
        if ($para.Count -and ($isOpen -or $isHeading -or -not $t)) {
            $body = ($para -join "`n").Trim()
            $kind = if (-not $Markdown) { $PlainKind } elseif ($body -match '(?m)^(?!\s*\|)') { 'prose' } else { 'lines' }
            [void]$blocks.Add(@($kind, $body, '')); $para.Clear()
        }
        if ($isOpen) { $fence = $Matches[1]; $closeFence = '^' + [regex]::Escape($fence[0]) + '{' + $fence.Length + ',}$'; [void]$para.Add($line) }
        elseif ($isHeading) {
            $label = ($t -replace '^#{1,6}\s+', '') -replace '\s+#+$', ''
            [void]$blocks.Add(@('heading', $t, $label.Substring(0, [math]::Min(120, $label.Length))))
        }
        elseif ($t) { [void]$para.Add($line) }
    }
    if ($para.Count) { [void]$blocks.Add(@('lines', ($para -join "`n").Trim(), '')) }    # unclosed fence runs to the end
    , $blocks
}

# Cuts text longer than $Size into pieces of at most $Size chars. 'lines' blocks: at the last line end.
# 'prose': at the last sentence end in the second half of the window, else the last whitespace.
# A single token longer than $Size is cut at exactly $Size.
function Split-LongText([string]$Text, [int]$Size, [string]$Kind = 'prose') {
    $pos = 0; $n = $Text.Length
    $newlines = [char[]]"`r`n"; $spaces = [char[]]" `t`r`n"
    while ($n - $pos -gt $Size) {
        $window = $Text.Substring($pos, $Size + 1)
        $cut = -1; $skip = $spaces
        if ($Kind -eq 'lines') { $nl = $window.LastIndexOf("`n"); if ($nl -gt 0) { $cut = $nl; $skip = $newlines } }   # keep code indentation
        else { $ends = [regex]::Matches($window, '[.!?](?=\s)'); if ($ends.Count -and $ends[$ends.Count - 1].Index -ge $Size / 2) { $cut = $ends[$ends.Count - 1].Index + 1 } }
        if ($cut -lt 0) { $cut = $window.LastIndexOfAny([char[]]" `t`n"); if ($cut -le 0) { $cut = $Size } }
        $piece = $Text.Substring($pos, $cut).TrimEnd(); if ($piece) { $piece }
        $pos += $cut
        while ($pos -lt $n -and $skip -contains $Text[$pos]) { $pos++ }
    }
    if ($pos -lt $n) { $Text.Substring($pos) }
}

# Tail of a finished chunk (at most $Overlap - 2 chars) repeated at the start of the next chunk in the same
# section. Starts at the first line start when the window holds table or fence lines (or $Kind is 'lines'),
# otherwise at the first sentence start; else the first word start; else there is no overlap.
function Get-OverlapTail([string]$Text, [int]$Overlap, [string]$Kind = 'prose') {
    $max = $Overlap - 2
    if ($max -le 0) { return '' }
    if ($Text.Length -le $max) { return $Text }
    $window = $Text.Substring($Text.Length - $max)
    $patterns = if ($Kind -eq 'lines' -or $window -match '(?m)^\s*(\||```|~~~)') { @('\n+(?=[^\n])') } else { @('(?<=[.!?])\s+(?=\S)', '\s+(?=\S)') }
    foreach ($pattern in $patterns) {
        $m = [regex]::Match($window, $pattern); if ($m.Success) { return $window.Substring($m.Index + $m.Length) }
    }
    ''
}

# Chunks never cross a heading and are at most $Size + $Overlap chars (except a single token longer than
# $Size). Within a section, paragraphs are packed up to $Size and each chunk after the first starts with an
# overlap tail of the previous one. Headings with no body are kept as leading text of the next section,
# flushed as their own chunk once they would push it past the bound.
function Split-Doc([string]$Rel, [string]$Text, [int]$Size = 1200, [int]$Overlap = 200) {
    $Size = [math]::Max(1, $Size); $Overlap = [math]::Max(0, $Overlap)
    $chunks = New-Object System.Collections.ArrayList
    $heading = ''; $buf = ''; $hasBody = $false; $carry = ''; $lastKind = 'prose'
    $plainKind = if ($Rel -match '\.txt$') { 'prose' } else { 'lines' }
    foreach ($b in (Get-DocBlock $Text ($Rel -match '\.(md|markdown)$') $plainKind)) {
        $kind = $b[0]; $text = $b[1]; $isHeading = $kind -eq 'heading'
        if ($isHeading) {
            if ($hasBody) { [void]$chunks.Add(@($heading, $buf)); $buf = '' }
            $hasBody = $false; $carry = ''; $kind = 'prose'
        }
        $pieces = if ($text.Length -gt $Size) { Split-LongText $text $Size $kind } else { $text }
        foreach ($piece in $pieces) {
            $limit = if ($hasBody -and $buf.Length -gt $Overlap - 2) { $Size } else { $Size + $Overlap }   # tiny or heading-only buffers may merge up to the bound
            if ($buf -and $buf.Length + 2 + $piece.Length -gt $limit) {
                [void]$chunks.Add(@($heading, $buf))
                $carry = if ($hasBody) { Get-OverlapTail $buf $Overlap $lastKind } else { '' }
                $buf = ''
            }
            $prefix = if ($buf) { $buf } else { $carry }
            $buf = if ($prefix) { "$prefix`n`n$piece" } else { $piece }
            if (-not $isHeading) { $hasBody = $true; $lastKind = $kind }
        }
        if ($isHeading) { $heading = $b[2] }                   # after flushing, so earlier headings keep their own label
    }
    if ($buf) { [void]$chunks.Add(@($heading, $buf)) }
    $i = 0
    foreach ($c in $chunks) { [pscustomobject]@{ id = "$Rel#$i"; file = $Rel; heading = $c[0]; text = $c[1] }; $i++ }
}

function Update-RagIndex {
    $docs = "$RagRoot\docs"; $idxFile = "$RagRoot\index.json"
    New-Item -ItemType Directory -Force $docs | Out-Null
    $docs = (Get-Item -LiteralPath $docs).FullName.TrimEnd('\')    # long-name form, as Get-ChildItem reports children
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
