# Terminal chat.
#  - If the server is running: chats through the API (no second model load), with think routing, RAG and schemas.
#  - Otherwise: -Direct loads the model in llama-cli (uses the model's own RAM).
# Usage: powershell -ExecutionPolicy Bypass -File C:\llm\scripts\chat.ps1 [-Rag off|on|auto] [-Direct -Model 27b|35b]
# Commands: /exit  /reset  /think on|off|auto  /rag on|off|auto  /schema <file>|off  /show (toggle thinking display)
#           /correct <right answer>  (records the last Q&A as a correction, re-indexed for RAG immediately)
#           Prefix a message with /think or /nothink to override the router for that message.
param([switch]$Direct, [ValidateSet('27b','35b')][string]$Model = '35b', [ValidateSet('off','on','auto')][string]$Rag = 'off')
$models = @{ '27b' = 'Qwen3.6-27B-Q4_K_M.gguf'; '35b' = 'Qwen3.6-35B-A3B-UD-Q4_K_M.gguf' }
if ($Direct) {
    & C:\llm\llama.cpp\cpu\llama-cli.exe -m "C:\llm\models\$($models[$Model])" -c 8192 -t 10 --jinja `
        --reasoning off --temp 0.7 --top-p 0.8 --top-k 20 --min-p 0
    return
}
. "$PSScriptRoot\lib.ps1"
$prof = Get-ServerProfile
if (-not (Test-Server $prof.Port)) { Write-Host "Server not running. Start it with start.ps1, or use -Direct."; return }
$history = New-Object System.Collections.ArrayList
$thinkMode = 'auto'; $schema = ''; $show = $false
Write-Host "Chatting with $($prof.Alias) [profile $($prof.Profile)] (/exit, /reset, /think on|off|auto, /rag on|off|auto, /schema file|off, /show, /correct <answer>)"
while ($true) {
    $q = Read-Host "`nyou"
    if ($q -eq '/exit') { break }
    if ($q -eq '/reset') { $history.Clear(); continue }
    if ($q -eq '/show') { $show = -not $show; Write-Host "show thinking: $show"; continue }
    if ($q -match '^/think\s+(on|off|auto)$') { $thinkMode = $Matches[1]; Write-Host "think: $thinkMode"; continue }
    if ($q -match '^/rag\s+(on|off|auto)$') { $Rag = $Matches[1]; Write-Host "rag: $Rag"; continue }
    if ($q -match '^/schema\s+(.+)$') { $schema = if ($Matches[1] -eq 'off') { '' } else { $Matches[1] }; Write-Host "schema: $schema"; continue }
    if ($q -match '^/correct\s+(.+)$') {
        if ($history.Count -lt 2) { Write-Host "Nothing to correct yet."; continue }
        Import-Module "$PSScriptRoot\rag.psm1" -DisableNameChecking -Force
        $id = Add-Correction -Question $history[-2].content -Correct $Matches[1] -Wrong $history[-1].content
        Write-Host "Saved correction ($id)."
        continue
    }

    try { $r = Invoke-Ask -Prompt $q -ThinkMode $thinkMode -RagMode $Rag -SchemaFile $schema -History $history -ServerProfile $prof }
    catch { Write-Host "Request failed: $($_.Exception.Message)" -ForegroundColor Red; continue }
    # History keeps the plain question (not injected sources) so the context stays small.
    [void]$history.Add(@{ role = 'user'; content = $r.Question })
    [void]$history.Add(@{ role = 'assistant'; content = $r.Content })
    Write-Host ("[think: {0} (score {1}){2}]" -f $(if ($r.Think) { 'yes' } else { 'no' }), $r.ThinkScore, $(if ($r.Sources.Count) { " [sources: $($r.Sources -join '; ')]" } else { '' })) -ForegroundColor DarkGray
    if ($show -and $r.Reasoning) { Write-Host $r.Reasoning -ForegroundColor DarkYellow }
    Write-Host "`nqwen> $($r.Content)"
    Write-Host ("[prompt {0} tok {1:N1}s | gen {2} tok {3:N2} tok/s | wall {4:N1}s]" -f $r.PromptTokens, ($r.PromptMs / 1000), $r.GenTokens, $r.GenTps, ($r.WallMs / 1000)) -ForegroundColor DarkGray
}
