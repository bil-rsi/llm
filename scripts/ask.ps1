# One-shot question to the running server with think routing, optional RAG and structured output.
# Usage: ask.ps1 "question" [-Think|-NoThink] [-Rag on|auto] [-Schema C:\llm\schemas\classify.json] [-Grammar file.gbnf]
#        [-ShowThinking] [-Raw] [-Seed N] [-MaxTokens N]
# Prefix the prompt with /think or /nothink to override the router.
param([Parameter(Mandatory, Position = 0)][string]$Prompt, [switch]$Think, [switch]$NoThink,
      [ValidateSet('off', 'on', 'auto')][string]$Rag = 'off', [string]$Schema = '', [string]$Grammar = '',
      [switch]$ShowThinking, [switch]$Raw, [int]$Seed = -1, [int]$MaxTokens = -1)
. "$PSScriptRoot\lib.ps1"
$prof = Get-ServerProfile
if (-not (Test-Server $prof.Port)) { Write-Host "Server not running. Start it with start.ps1."; exit 1 }
$mode = if ($Think) { 'on' } elseif ($NoThink) { 'off' } else { 'auto' }

$r = Invoke-Ask -Prompt $Prompt -ThinkMode $mode -RagMode $Rag -SchemaFile $Schema -GrammarFile $Grammar `
    -Seed $Seed -MaxTokens $MaxTokens -ServerProfile $prof
if ($Raw) { $r.Content; return }

Write-Host ("[{0} | think: {1} (score {2}: {3})]" -f $prof.Alias, $(if ($r.Think) { 'yes' } else { 'no' }), $r.ThinkScore, ($r.ThinkReasons -join ', ')) -ForegroundColor DarkGray
if ($r.Sources.Count) { Write-Host "[sources: $($r.Sources -join '; ')]" -ForegroundColor DarkGray }
if ($ShowThinking -and $r.Reasoning) { Write-Host "`n--- thinking ---`n$($r.Reasoning)`n----------------" -ForegroundColor DarkYellow }
Write-Host "`n$($r.Content)"
if ($Schema) { Write-Host ("[json valid: {0}, retries: {1}]" -f $r.JsonValid, $r.Retries) -ForegroundColor DarkGray }
Write-Host ("[{0} call(s) | prompt {1} tok {2:N1}s | gen {3} tok {4:N2} tok/s | wall {5:N1}s{6}]" -f $r.Calls, $r.PromptTokens, ($r.PromptMs / 1000),
    $r.GenTokens, $r.GenTps, ($r.WallMs / 1000), $(if ($r.DraftN) { " | draft {0}/{1}" -f $r.DraftAccepted, $r.DraftN } else { '' })) -ForegroundColor DarkGray
