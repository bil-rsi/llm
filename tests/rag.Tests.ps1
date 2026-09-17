# Pester 3.4 tests for scripts\rag.psm1. Run: Invoke-Pester C:\llm\tests
Import-Module "$PSScriptRoot\..\scripts\rag.psm1" -DisableNameChecking -Force
$rag = Get-Module rag

function Split-TestDoc([string]$Text, [int]$Size = 1200, [int]$Overlap = 200) { ,@(& $rag { param($t, $s, $o) Split-Doc 'doc.md' $t $s $o } $Text $Size $Overlap) }
function Use-TempRagRoot([string]$Root) { New-Item -ItemType Directory -Force "$Root\docs" | Out-Null; & $rag { param($r) $script:RagRoot = $r; $script:Index = $null } $Root }
function Read-Index([string]$Root) { Get-Content "$Root\index.json" -Raw | ConvertFrom-Json }
function Write-Index([string]$Root, $Index) { $Index | ConvertTo-Json -Depth 5 -Compress | Set-Content "$Root\index.json" -Encoding UTF8 }

Describe 'Split-Doc document order and headings' {
    It 'emits buffered text before a following long paragraph (B1)' {
        $long = (1..60 | ForEach-Object { "Sentence number $_ is here." }) -join ' '
        $chunks = Split-TestDoc "# A`n`nintro paragraph BEFORE`n`n$long"
        $chunks[0].text | Should Match 'intro paragraph BEFORE'
    }

    It 'keeps every paragraph in source order' {
        $rand = New-Object Random 7
        $text = (1..25 | ForEach-Object { "mark$('{0:D2}' -f $_) " + ('filler words go here ' * $rand.Next(1, 90)) }) -join "`n`n"
        $chunks = Split-TestDoc $text
        $first = 1..25 | ForEach-Object { $m = "mark$('{0:D2}' -f $_)"; for ($i = 0; $i -lt $chunks.Count; $i++) { if ($chunks[$i].text.Contains($m)) { $i; break } } }
        @($first).Count | Should Be 25
        for ($k = 1; $k -lt 25; $k++) { $first[$k] | Should Not BeLessThan $first[$k - 1] }
    }

    It 'labels each chunk with the heading its first line falls under (B3)' {
        $chunks = Split-TestDoc ("# Alpha`n`n" + ('alpha text ' * 100).Trim() + "`n`n# Beta`n`n" + ('beta text ' * 100).Trim())
        foreach ($c in $chunks) {
            if ($c.heading -eq 'Beta') { $c.text | Should Not Match 'alpha' } else { $c.heading | Should Be 'Alpha'; $c.text | Should Not Match 'beta' }
        }
        @($chunks | Where-Object heading -eq 'Beta').Count | Should BeGreaterThan 0
    }

    It 'starts a new chunk at every heading, even for short sections' {
        $chunks = Split-TestDoc "intro without heading`n`n# One`n`nshort one`n`n## Two`n`nshort two"
        ($chunks | ForEach-Object { $_.heading }) -join '|' | Should Be '|One|Two'
        $chunks[1].text | Should Be "# One`n`nshort one"
    }

    It 'returns no chunks for empty or whitespace-only text' {
        (Split-TestDoc '').Count | Should Be 0
        (Split-TestDoc "  `n`n  `n").Count | Should Be 0
    }

    It 'assigns sequential ids' {
        $chunks = Split-TestDoc "# One`n`na`n`n# Two`n`nb"
        ($chunks | ForEach-Object { $_.id }) -join ',' | Should Be 'doc.md#0,doc.md#1'
    }
}

Describe 'Split-Doc chunk boundaries' {
    function Get-Word([string]$Text) { @($Text -split '\s+' | Where-Object { $_ }) }
    $sentences = (1..400 | ForEach-Object { "Topic$_ has detail number $($_ * 7) in it." })

    It 'never starts or ends a chunk mid-word when packing paragraphs (B2)' {
        $text = (0..39 | ForEach-Object { $sentences[($_ * 10)..($_ * 10 + 4)] -join ' ' }) -join "`n`n"
        $words = New-Object 'System.Collections.Generic.HashSet[string]' -ArgumentList (,[string[]](Get-Word $text))
        $chunks = Split-TestDoc $text
        $chunks.Count | Should BeGreaterThan 2
        foreach ($c in $chunks) { $w = Get-Word $c.text; $words.Contains($w[0]) | Should Be $true; $words.Contains($w[-1]) | Should Be $true }
    }

    It 'starts the overlap at a sentence start when one is available' {
        $text = (0..39 | ForEach-Object { $sentences[($_ * 10)..($_ * 10 + 4)] -join ' ' }) -join "`n`n"
        foreach ($c in (Split-TestDoc $text | Select-Object -Skip 1)) { $c.text | Should Match '^Topic\d+ has' }
    }

    It 'splits a long paragraph at sentence ends within the size limit (B5)' {
        $chunks = Split-TestDoc ($sentences[0..160] -join ' ')
        $chunks.Count | Should BeGreaterThan 3
        foreach ($c in $chunks) { $c.text.Length | Should Not BeGreaterThan 1400; $c.text | Should Match '^Topic\d+ has'; $c.text | Should Match 'in it\.$' }
    }

    It 'keeps all text of a long paragraph without sentence punctuation, cutting only at spaces' {
        $text = ((1..900) | ForEach-Object { "w$_" }) -join ' '
        $chunks = Split-TestDoc $text
        foreach ($c in $chunks) { $c.text.Length | Should Not BeGreaterThan 1400; $c.text | Should Match '^w\d+ '; $c.text | Should Match ' w\d+$' }
        $seen = @{}; foreach ($c in $chunks) { foreach ($w in Get-Word $c.text) { $seen[$w] = 1 } }
        $seen.Count | Should Be 900
    }

    It 'hard-cuts a single token longer than the chunk size without losing characters' {
        $blob = 'x' * 3000
        $chunks = Split-TestDoc "before`n`n$blob`n`nafter"
        foreach ($c in $chunks) { $c.text.Length | Should Not BeGreaterThan 1400 }
        (($chunks | ForEach-Object { $_.text }) -join '' -replace '[^x]', '').Length | Should Not BeLessThan 3000
        $chunks[0].text | Should Match '^before'
        $chunks[-1].text | Should Match 'after$'
    }
}

Describe 'Split-Doc code fences and tables' {
    function Get-Line([string]$Text) { @($Text -split '\r?\n' | Where-Object { $_.Trim() }) }
    $filler = (1..32 | ForEach-Object { "Filler sentence $_ goes here." }) -join ' '     # ~920 chars: filler + fence > 1200, fence alone fits

    It 'keeps a fenced block with blank lines in one chunk when it fits' {
        $fence = "``````powershell`n" + ((1..12 | ForEach-Object { "Get-Thing -Id $_ | Out-Null`n" }) -join "`n") + "``````"
        $chunks = Split-TestDoc "# Code`n`n$filler`n`n$fence`n`nafter text"
        @($chunks | Where-Object { $_.text.Contains($fence) }).Count | Should Be 1
    }

    It 'does not treat # lines inside a fence as headings' {
        $chunks = Split-TestDoc "# Title`n`n``````powershell`n# not a heading`nGet-Thing`n```````n`n## Next`n`ntext"
        ($chunks | ForEach-Object { $_.heading }) -join '|' | Should Be 'Title|Next'
        $chunks[0].text | Should Match '# not a heading'
    }

    It 'splits a fence longer than the chunk size only at line ends' {
        $lines = 1..120 | ForEach-Object { "    Invoke-Step -Name step$_ -Verbose" }
        $source = New-Object 'System.Collections.Generic.HashSet[string]' -ArgumentList (,[string[]]($lines + "``````powershell" + "``````"))
        $chunks = Split-TestDoc ("``````powershell`n" + ($lines -join "`n") + "`n``````")
        $chunks.Count | Should BeGreaterThan 2
        foreach ($c in $chunks) { $c.text.Length | Should Not BeGreaterThan 1400; foreach ($l in Get-Line $c.text) { $source.Contains($l) | Should Be $true } }
    }

    It 'splits a long table only at row ends, overlap included' {
        $rows = 1..70 | ForEach-Object { "| ``cmd$_.ps1 -Flag e.g. value`` | Does thing number $_. Then more. |" }
        $source = New-Object 'System.Collections.Generic.HashSet[string]' -ArgumentList (,[string[]]($rows + '| Command | Purpose |' + '|---|---|'))
        $chunks = Split-TestDoc ("| Command | Purpose |`n|---|---|`n" + ($rows -join "`n"))
        $chunks.Count | Should BeGreaterThan 2
        foreach ($c in $chunks) { foreach ($l in Get-Line $c.text) { $source.Contains($l) | Should Be $true } }
    }

    It 'runs an unclosed fence to the end of the document' {
        $chunks = Split-TestDoc "# T`n`n``````text`nline one`n`n# still code`nline two"
        $chunks.Count | Should Be 1
        $chunks[0].text | Should Match 'line two$'
    }
}

Describe 'Split-Doc keeps all text' {
    function Get-MissingLine([string]$Rel, [string]$Text) {
        $all = (@(& $rag { param($r, $t) Split-Doc $r $t } $Rel $Text) | ForEach-Object { $_.text }) -join "`n"
        @($Text -split '\r?\n' | Where-Object { $_.Trim() -and -not $all.Contains($_.Trim()) })
    }

    It 'keeps consecutive and trailing markdown headings' {
        Get-MissingLine 'doc.md' "# Parent`n`n## Child`n`nbody text`n`n## Empty at end" | Should BeNullOrEmpty
    }

    It 'does not treat # comments in code files as headings (review finding)' {
        $code = "# Usage: ask.ps1 question`n# Second comment`nparam([string]`$Prompt)`n`n# Section comment`nInvoke-Thing `$Prompt"
        $chunks = @(& $rag { param($t) Split-Doc 'scripts/ask.ps1' $t } $code)
        $chunks.Count | Should Be 1
        $chunks[0].heading | Should Be ''
        Get-MissingLine 'scripts/ask.ps1' $code | Should BeNullOrEmpty
    }

    It 'keeps every line of the real scripts and fixture docs' {
        foreach ($f in @(Get-ChildItem "$PSScriptRoot\..\scripts\*.ps1") + @(Get-ChildItem "$PSScriptRoot\fixtures\rag\docs\*.md")) {
            Get-MissingLine $f.Name ([IO.File]::ReadAllText($f.FullName)) | Should BeNullOrEmpty
        }
    }
}

Describe 'Update-RagIndex relative file names' {
    $long = "$TestDrive\long-folder-name-for-8dot3"
    New-Item -ItemType Directory -Force "$long\docs\sub" | Out-Null
    Set-Content "$long\docs\models.md" 'model notes' -Encoding UTF8
    Set-Content "$long\docs\sub\notes.txt" 'sub notes' -Encoding UTF8
    $short = (New-Object -ComObject Scripting.FileSystemObject).GetFolder((Get-Item $long).FullName).ShortPath

    foreach ($case in @(@{ Name = 'a long path'; Root = (Get-Item $long).FullName }, @{ Name = 'an 8.3 short path'; Root = $short }, @{ Name = 'a trailing backslash'; Root = "$long\" })) {
        It "stores paths relative to docs when the root is $($case.Name)" {
            Use-TempRagRoot $case.Root
            Update-RagIndex 6>$null
            ((Read-Index $case.Root).files.PSObject.Properties.Name | Sort-Object) -join ',' | Should Be 'models.md,sub/notes.txt'
        }
    }
}

Describe 'Update-RagIndex chunker version' {
    $version = & $rag { $ChunkerVersion }
    # Each test gets its own freshly indexed root, so tests don't depend on each other's index state.
    function New-IndexedRoot([string]$Name) {
        $root = "$TestDrive\$Name"; Use-TempRagRoot $root
        Set-Content "$root\docs\a.md" "# Title`n`nSome text about ports and servers." -Encoding UTF8
        Update-RagIndex 6>$null
        $root
    }
    function Set-StaleChunk([string]$Root, [scriptblock]$Edit) { $idx = Read-Index $Root; $idx.chunks[0].text = 'STALE'; & $Edit $idx; Write-Index $Root $idx }

    It 'records the chunker version in index.json' {
        $root = New-IndexedRoot 'records'
        $version | Should BeGreaterThan 1
        (Read-Index $root).chunker | Should Be $version
    }

    It 're-chunks unchanged files when the index has no chunker version' {
        $root = New-IndexedRoot 'noversion'
        Set-StaleChunk $root { param($i) $i.PSObject.Properties.Remove('chunker') }
        $out = ((Update-RagIndex 6>&1 | ForEach-Object { "$_" }) -join ' ')
        (Read-Index $root).chunks[0].text | Should Not Be 'STALE'
        $out | Should Match "chunker v1 -> v$version, rebuilt all"
    }

    It 're-chunks unchanged files when the chunker version differs' {
        $root = New-IndexedRoot 'oldversion'
        Set-StaleChunk $root { param($i) $i.chunker = $version - 1 }
        $out = ((Update-RagIndex 6>&1 | ForEach-Object { "$_" }) -join ' ')
        (Read-Index $root).chunks[0].text | Should Not Be 'STALE'
        $out | Should Match "chunker v$($version - 1) -> v$version, rebuilt all"
    }

    It 'reuses chunks of unchanged files when the version matches' {
        $root = New-IndexedRoot 'reuse'
        Set-StaleChunk $root { param($i) }
        $out = ((Update-RagIndex 6>&1 | ForEach-Object { "$_" }) -join ' ')
        (Read-Index $root).chunks[0].text | Should Be 'STALE'
        $out | Should Match '\(0 changed\)'
    }

    It 're-chunks a file whose modification time changed' {
        $root = New-IndexedRoot 'mtime'
        Set-StaleChunk $root { param($i) }
        (Get-Item "$root\docs\a.md").LastWriteTimeUtc = (Get-Date).ToUniversalTime().AddMinutes(1)
        $out = ((Update-RagIndex 6>&1 | ForEach-Object { "$_" }) -join ' ')
        (Read-Index $root).chunks[0].text | Should Not Be 'STALE'
        $out | Should Match '\(1 changed\)'
    }
}

Describe 'Split-Doc size bounds' {
    function Assert-Bound($Chunks, [int]$Max) { foreach ($c in $Chunks) { $c.text.Length | Should Not BeGreaterThan $Max } }
    $prose = (1..40 | ForEach-Object { "Sentence $_ is a fairly ordinary sentence." }) -join ' '

    It 'bounds a chunk that follows many body-less headings (ship blocker)' {
        $h = (1..15 | ForEach-Object { "## Empty heading number $_ with a long title" }) -join "`n`n"
        Assert-Bound (Split-TestDoc "$h`n`n$prose") 1400
    }

    It 'bounds a long run of headings and keeps every line' {
        $text = ((1..2000 | ForEach-Object { "# navigation link heading $_" }) -join "`n") + "`n`nquokka body"
        $chunks = Split-TestDoc $text
        Assert-Bound $chunks 1400
        $all = ($chunks | ForEach-Object { $_.text }) -join "`n"
        foreach ($n in 1, 777, 2000) { $all.Contains("# navigation link heading $n") | Should Be $true }
        $all.Contains('quokka body') | Should Be $true
    }

    It 'bounds a heading line longer than the chunk size and caps the heading label' {
        $chunks = Split-TestDoc ("# " + ('longword ' * 300).Trim() + "`n`nbody")
        Assert-Bound $chunks 1400
        foreach ($c in $chunks) { $c.heading.Length | Should Not BeGreaterThan 120 }
    }

    It 'keeps packed paragraph chunks within Size + Overlap for default and custom sizes' {
        $t = (1..60 | ForEach-Object { "Para$_ sentence one here. Sentence two here." }) -join "`n`n"
        Assert-Bound (Split-TestDoc $t) 1400
        Assert-Bound (Split-TestDoc $t 300 60) 360
    }

    It 'keeps mixed prose and table chunks within Size + Overlap with whole rows' {
        $rows = (1..20 | ForEach-Object { "| row$_ | value $_ |" }) -join "`n"
        $chunks = Split-TestDoc ((1..6 | ForEach-Object { "$prose`n`n$rows" }) -join "`n`n")
        Assert-Bound $chunks 1400
        foreach ($c in $chunks) { foreach ($l in ($c.text -split "`n" | Where-Object { $_.StartsWith('|') })) { $l | Should Match '^\| row\d+ \| value \d+ \|$' } }
    }

    It 'handles Overlap 0, Overlap >= Size and Size 0 without hanging' {
        $noOverlap = Split-TestDoc $prose 300 0
        foreach ($c in ($noOverlap | Select-Object -Skip 1)) { $c.text | Should Match '^Sentence \d+ is' }
        (($noOverlap | ForEach-Object { $_.text }) -join ' ' -split '\s+').Count | Should Be ($prose -split '\s+').Count
        Assert-Bound (Split-TestDoc ($prose * 2) 200 300) 500
        (Split-TestDoc 'abc def' 0 0).Count | Should BeGreaterThan 0
    }
}

Describe 'Split-Doc overlap and cut rules' {
    It 'repeats the tail of the previous chunk at the start of the next' {
        $c = Split-TestDoc ((1..60 | ForEach-Object { "Para$_ sentence one here. Sentence two here." }) -join "`n`n")
        $c.Count | Should BeGreaterThan 2
        for ($i = 1; $i -lt $c.Count; $i++) { $head = ($c[$i].text -split "`n`n")[0]; $head.Length | Should BeGreaterThan 10; $c[$i - 1].text.Contains($head) | Should Be $true }
    }

    It 'prefers a sentence end in the second half of the window' {
        $c = Split-TestDoc ('Short. ' + ('word ' * 300).Trim()) 200 0
        $c[0].text | Should Not Be 'Short.'
        $c[0].text.Length | Should BeGreaterThan 100
    }

    It 'starts the overlap on a whole table row when a short sentence follows the table' {
        $rows = 1..30 | ForEach-Object { "| cmd$_ | Does thing $_. Then more. |" }
        $source = New-Object 'System.Collections.Generic.HashSet[string]' -ArgumentList (,[string[]]$rows)
        $text = ($rows -join "`n") + "`n`nShort note.`n`n" + ((1..30 | ForEach-Object { "Prose sentence $_ follows here." }) -join ' ')
        foreach ($c in Split-TestDoc $text) { foreach ($l in ($c.text -split "`n" | Where-Object { $_.StartsWith('|') -or $_.EndsWith('|') })) { $source.Contains($l) | Should Be $true } }
    }

    It 'cuts .txt paragraphs at sentence ends and code files only at line ends' {
        $l = (1..60 | ForEach-Object { "line number $_ has some words. And a second sentence here" }) -join "`n"
        foreach ($c in @(& $rag { param($t) Split-Doc 'a.ps1' $t } $l)) { $c.text | Should Match 'sentence here$' }
        @(& $rag { param($t) Split-Doc 'a.txt' $t } $l | Where-Object { $_.text -match 'some words\.$' }).Count | Should BeGreaterThan 0
    }

    It 'produces the same chunks for CRLF and LF input' {
        $lf = "# A`n`npara one`nline two`n`n``````ps`ncode`n`nmore`n```````n`n# B`n`n" + ((1..60 | ForEach-Object { "Bee sentence $_ here." }) -join ' ')
        $key = { param($cs) ($cs | ForEach-Object { $_.heading + '|' + $_.text }) -join '#' }
        (& $key (Split-TestDoc ($lf -replace "`n", "`r`n"))) | Should Be (& $key (Split-TestDoc $lf))
    }

    It 'keeps paragraph and fence order across headings' {
        $long = (1..60 | ForEach-Object { "Filler $_ sentence." }) -join ' '
        $chunks = Split-TestDoc "# H1`n`nmark01 para`n`n``````text`nmark02 code`n`nstill code`n```````n`n# H2`n`nmark03 $long`n`nmark04 end"
        $first = 1..4 | ForEach-Object { $m = "mark0$_"; for ($i = 0; $i -lt $chunks.Count; $i++) { if ($chunks[$i].text.Contains($m)) { $i; break } } }
        @($first).Count | Should Be 4
        for ($k = 1; $k -lt 4; $k++) { $first[$k] | Should Not BeLessThan $first[$k - 1] }
    }
}

Describe 'Split-Doc fence closing' {
    It 'does not close a four-backtick fence on an inner three-backtick line' {
        $chunks = Split-TestDoc "# Doc`n`n````````markdown`n``````bash`n# install step`nnpm i`n```````n`````````n`n# After`n`ntext"
        ($chunks | ForEach-Object { $_.heading }) -join '|' | Should Be 'Doc|After'
    }

    It 'does not close a backtick fence on a tilde line or a line with an info string' {
        $chunks = Split-TestDoc "# Doc`n`n``````bash`n~~~`n# comment in bash`n``````python`necho`n```````n`n# After`n`ntext"
        ($chunks | ForEach-Object { $_.heading }) -join '|' | Should Be 'Doc|After'
    }
}

Describe 'Search, prompt and tokens' {
    It 'returns chunks with id, file, heading and text' {
        (Split-TestDoc "# A`n`nx")[0].PSObject.Properties.Name -join ',' | Should Be 'id,file,heading,text'
    }

    It 'ranks the matching chunk first and honours K and no-match' {
        $root = "$TestDrive\search"; Use-TempRagRoot $root
        Set-Content "$root\docs\a.md" "# Ports`n`nThe server port is 8080 for the web UI." -Encoding UTF8
        Set-Content "$root\docs\b.md" "# Cooking`n`nBoil pasta in salted water." -Encoding UTF8
        Set-Content "$root\docs\c.md" "# Server`n`nThe server runs on the laptop." -Encoding UTF8
        Update-RagIndex 6>$null
        (Search-Rag 'server port' -MinRel 0)[0].Chunk.file | Should Be 'a.md'
        @(Search-Rag 'server port' -K 1 -MinRel 0).Count | Should Be 1
        @(Search-Rag 'zzzzqqq').Count | Should Be 0
    }

    It 'numbers sources and handles no hits in the prompt' {
        Format-RagPrompt 'q' @() | Should Match 'no matching documents found'
        $hit = [pscustomobject]@{ Chunk = [pscustomobject]@{ file = 'a.md'; heading = 'H'; text = 'T' } }
        $p = Format-RagPrompt 'q' @($hit)
        $p | Should Match '\[1\] a\.md > H'
        $p | Should Match 'Question: q$'
    }

    It 'drops stopwords and single characters from tokens' {
        (Get-Tokens 'The port is 8080 a x') -join ',' | Should Be 'port,8080'
    }
}
