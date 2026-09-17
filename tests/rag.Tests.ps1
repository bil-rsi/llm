# Pester 3.4 tests for scripts\rag.psm1. Run: Invoke-Pester C:\llm\tests
Import-Module "$PSScriptRoot\..\scripts\rag.psm1" -DisableNameChecking -Force
$rag = Get-Module rag

function Split-TestDoc([string]$Text, [int]$Size = 1200, [int]$Overlap = 200) { @(& $rag { param($t, $s, $o) Split-Doc 'doc.md' $t $s $o } $Text $Size $Overlap) }
function Use-TempRagRoot([string]$Root) { New-Item -ItemType Directory -Force "$Root\docs" | Out-Null; & $rag { param($r) $script:RagRoot = $r; $script:Index = $null } $Root }
function Read-Index([string]$Root) { Get-Content "$Root\index.json" -Raw | ConvertFrom-Json }
function Write-Index([string]$Root, $Index) { $Index | ConvertTo-Json -Depth 5 -Compress | Set-Content "$Root\index.json" -Encoding UTF8 }

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
