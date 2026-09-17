# Builds/refreshes the BM25 index for C:\llm\rag\docs (only changed files are re-chunked).
# Usage: rag-index.ps1            then  ask.ps1 -Rag on "question"
#        rag-index.ps1 -Query "text" [-K 4]   to inspect what retrieval returns
param([string]$Query = '', [int]$K = 4, [int]$MaxTokens = 1500)
Import-Module "$PSScriptRoot\rag.psm1" -DisableNameChecking -Force
if (-not $Query) { Update-RagIndex; return }
foreach ($h in Search-Rag $Query -K $K -MaxTokens $MaxTokens) {
    Write-Host ("{0,6:N2}  {1}  {2}" -f $h.Score, $h.Chunk.id, $h.Chunk.heading) -ForegroundColor Cyan
    Write-Host ($h.Chunk.text.Substring(0, [math]::Min(200, $h.Chunk.text.Length)) + '...')
}
