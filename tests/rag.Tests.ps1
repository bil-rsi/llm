# Pester 3.4 tests for scripts\rag.psm1. Run: Invoke-Pester C:\llm\tests
Import-Module "$PSScriptRoot\..\scripts\rag.psm1" -DisableNameChecking -Force
$rag = Get-Module rag

function Split-TestDoc([string]$Text, [int]$Size = 1200, [int]$Overlap = 200) { @(& $rag { param($t, $s, $o) Split-Doc 'doc.md' $t $s $o } $Text $Size $Overlap) }
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
    $root = "$TestDrive\rag"
    Use-TempRagRoot $root
    Set-Content "$root\docs\a.md" "# Title`n`nSome text about ports and servers." -Encoding UTF8
    $version = & $rag { $ChunkerVersion }

    It 'records the chunker version in index.json' {
        Update-RagIndex 6>$null
        $version | Should BeGreaterThan 1
        (Read-Index $root).chunker | Should Be $version
    }

    It 're-chunks unchanged files when the index has no chunker version' {
        $idx = Read-Index $root; $idx.chunks[0].text = 'STALE'; $idx.PSObject.Properties.Remove('chunker'); Write-Index $root $idx
        $out = (Update-RagIndex 6>&1 | Out-String)
        (Read-Index $root).chunks[0].text | Should Not Be 'STALE'
        $out | Should Match 'rebuilt'
    }

    It 're-chunks unchanged files when the chunker version differs' {
        $idx = Read-Index $root; $idx.chunks[0].text = 'STALE'; $idx.chunker = $version - 1; Write-Index $root $idx
        Update-RagIndex 6>$null
        (Read-Index $root).chunks[0].text | Should Not Be 'STALE'
    }

    It 'reuses chunks of unchanged files when the version matches' {
        $idx = Read-Index $root; $idx.chunks[0].text = 'STALE'; Write-Index $root $idx
        $out = (Update-RagIndex 6>&1 | Out-String)
        (Read-Index $root).chunks[0].text | Should Be 'STALE'
        $out | Should Match '\(0 changed\)'
    }
}
