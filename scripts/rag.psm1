# Lightweight BM25 retrieval over C:\llm\rag\docs (no extra model, no GPU memory).
#   Update-RagIndex            re-chunks changed files into C:\llm\rag\index.json
#   Search-Rag $q -K 4 -MaxTokens 1500
#   Format-RagPrompt $q $hits  -> user message with numbered sources

$RagRoot = 'C:\llm\rag'
$Stop = @('the','a','an','and','or','of','to','in','on','for','is','are','was','were','be','it','this','that','with','as','at','by','from',
          'what','how','do','does','i','you','my','your','we','can','yang','dan','di','ke','dari','ini','itu','untuk','dengan','adalah','apa','bagaimana')
$StopSet = New-Object 'System.Collections.Generic.HashSet[string]' -ArgumentList (,[string[]]$Stop)
$Exts = @('.md', '.txt', '.ps1', '.py', '.js', '.ts', '.json', '.yaml', '.yml', '.csv', '.html', '.log')
$ChunkerVersion = 3           # bump whenever Split-Doc output changes: forces a full re-chunk of every file
$script:Index = $null

function Get-Tokens([string]$Text) {
    foreach ($t in [regex]::Split($Text.ToLowerInvariant(), '[^\p{L}\p{N}_]+')) { if ($t.Length -gt 1 -and -not $StopSet.Contains($t)) { $t } }
}

# Blocks = headings, blank-line-separated paragraphs and fenced code blocks, in document order.
# Kind 'lines' (fences, tables, code files) is only ever cut at line ends; 'prose' at sentence or word ends.
# Headings and fences are only recognised in markdown; other files are plain paragraphs of $PlainKind.
function Get-DocBlock([string]$Text, [bool]$Markdown = $true, [string]$PlainKind = 'prose') {
    $para = New-Object System.Collections.ArrayList; $inFence = $false
    $paraKind = if ($Markdown) { '' } else { $PlainKind }
    foreach ($line in ($Text -split '\r?\n') + '') {          # trailing '' flushes the last paragraph
        $isFence = $Markdown -and $line -match '^\s*(```|~~~)'
        if ($inFence) {
            [void]$para.Add($line)
            if ($isFence) { ConvertTo-DocBlock $para 'lines'; $para.Clear(); $inFence = $false }
            continue
        }
        $isHeading = $Markdown -and $line -match '^#{1,6}\s+\S'
        if (($isFence -or $isHeading -or -not $line.Trim()) -and $para.Count) { ConvertTo-DocBlock $para $paraKind; $para.Clear() }
        if ($isFence) { [void]$para.Add($line); $inFence = $true }
        elseif ($isHeading) { [pscustomobject]@{ Heading = ($line -replace '^#{1,6}\s+', '').Trim(); Kind = 'heading'; Text = $line.Trim() } }
        elseif ($line.Trim()) { [void]$para.Add($line) }
    }
    if ($para.Count) { ConvertTo-DocBlock $para 'lines' }            # unclosed fence runs to the end
}

# A paragraph whose every line is a table row counts as 'lines'.
function ConvertTo-DocBlock($Lines, [string]$Kind = '') {
    if (-not $Kind) { $Kind = if (@($Lines | Where-Object { -not $_.TrimStart().StartsWith('|') }).Count) { 'prose' } else { 'lines' } }
    [pscustomobject]@{ Heading = $null; Kind = $Kind; Text = ($Lines -join "`n").Trim() }
}

# Cuts text longer than $Size into pieces of at most $Size chars. 'lines' blocks: at the last line end.
# 'prose': at the last sentence end in the second half of the window, else the last whitespace.
# A single token longer than $Size is cut at exactly $Size.
function Split-LongText([string]$Text, [int]$Size, [string]$Kind = 'prose') {
    $rest = $Text
    while ($rest.Length -gt $Size) {
        $window = $rest.Substring(0, $Size + 1)
        $cut = $Size
        $ends = [regex]::Matches($window, '[.!?](?=\s)')
        $nl = $window.LastIndexOf("`n"); $ws = $window.LastIndexOfAny([char[]]" `t`n")
        if ($Kind -eq 'lines' -and $nl -gt 0) { $cut = $nl }
        elseif ($Kind -ne 'lines' -and $ends.Count -and $ends[$ends.Count - 1].Index -ge $Size / 2) { $cut = $ends[$ends.Count - 1].Index + 1 }
        elseif ($ws -gt 0) { $cut = $ws }
        $rest.Substring(0, $cut).TrimEnd()
        $rest = if ($Kind -eq 'lines') { $rest.Substring($cut).TrimStart("`r`n".ToCharArray()) } else { $rest.Substring($cut).TrimStart() }   # keep code indentation
    }
    if ($rest) { $rest }
}

# Tail of a finished chunk (at most $Overlap - 2 chars) repeated at the start of the next chunk in the same
# section. Starts at the first line start ('lines') or sentence start ('prose') inside the window, else the
# first word start, else there is no overlap.
function Get-OverlapTail([string]$Text, [int]$Overlap, [string]$Kind = 'prose') {
    $max = $Overlap - 2
    if ($max -le 0) { return '' }
    if ($Text.Length -le $max) { return $Text }
    $window = $Text.Substring($Text.Length - $max)
    $patterns = if ($Kind -eq 'lines') { @('\n+(?=[^\n])') } else { @('(?<=[.!?])\s+(?=\S)', '\s+(?=\S)') }
    foreach ($pattern in $patterns) {
        $m = [regex]::Match($window, $pattern); if ($m.Success) { return $window.Substring($m.Index + $m.Length) }
    }
    ''
}

# Chunks never cross a heading; within a section, paragraphs are packed up to $Size and each chunk
# after the first starts with an overlap tail of the previous one (so text <= $Size + $Overlap).
function Split-Doc([string]$Rel, [string]$Text, [int]$Size = 1200, [int]$Overlap = 200) {
    $chunks = New-Object System.Collections.ArrayList
    $heading = ''; $buf = ''; $hasBody = $false; $carry = ''; $lastKind = 'prose'
    $plainKind = if ($Rel -match '\.txt$') { 'prose' } else { 'lines' }
    foreach ($b in Get-DocBlock $Text ($Rel -match '\.(md|markdown)$') $plainKind) {
        if ($b.Kind -eq 'heading') {
            if ($hasBody) { [void]$chunks.Add(@{ heading = $heading; text = $buf }); $buf = '' }
            $buf = if ($buf) { "$buf`n`n$($b.Text)" } else { $b.Text }    # a heading with no body stays as text of the next section
            $heading = $b.Heading; $hasBody = $false; $carry = ''
            continue
        }
        foreach ($piece in @(if ($b.Text.Length -gt $Size) { Split-LongText $b.Text $Size $b.Kind } else { $b.Text })) {
            if ($hasBody -and $buf.Length + 2 + $piece.Length -gt $Size) {
                [void]$chunks.Add(@{ heading = $heading; text = $buf }); $carry = Get-OverlapTail $buf $Overlap $lastKind; $buf = ''
            }
            $lastKind = $b.Kind
            $prefix = if ($buf) { $buf } else { $carry }
            $buf = if ($prefix) { "$prefix`n`n$piece" } else { $piece }; $hasBody = $true
        }
    }
    if ($buf) { [void]$chunks.Add(@{ heading = $heading; text = $buf }) }
    $i = 0
    foreach ($c in $chunks) { [pscustomobject]@{ id = "$Rel#$i"; file = $Rel; heading = $c.heading; text = $c.text }; $i++ }
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
