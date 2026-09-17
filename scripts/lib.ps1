# Shared client helpers: dot-source with  . C:\llm\scripts\lib.ps1
# Think router, request builder (thinking/sampling/schema/grammar), API call, JSON reply validation.

$LlmRoot = 'C:\llm'

function Get-ServerProfile {
    $f = "$LlmRoot\logs\server.profile.json"
    if (Test-Path $f) { return Get-Content $f -Raw | ConvertFrom-Json }
    [pscustomobject]@{ Profile = 'unknown'; Model = ''; Alias = ''; Port = 8080; ThinkThreshold = 3; RagK = 3; RagTokens = 1200 }
}

function Test-Server([int]$Port = 8080) {
    try { return (Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 2).status -eq 'ok' } catch { return $false }
}

# Scores a prompt for "needs reasoning". Returns Think, Score, Reasons and the prompt with /think|/nothink removed.
function Get-ThinkDecision([string]$Prompt, [int]$Threshold = 3, [bool]$HasSchema = $false) {
    $text = $Prompt.Trim()
    if ($text -match '^/think\b')   { return [pscustomobject]@{ Think = $true;  Score = 99;  Reasons = @('forced'); Prompt = ($text -replace '^/think\s*', '') } }
    if ($text -match '^/nothink\b') { return [pscustomobject]@{ Think = $false; Score = -99; Reasons = @('forced'); Prompt = ($text -replace '^/nothink\s*', '') } }
    $score = 0; $reasons = New-Object System.Collections.ArrayList
    $kw = '\bprove|\bwhy\b|derive|calculat|how many|step by step|debug|optimi[sz]|compare|trade-?off|\bplan\b|\bdesign\b|algorithm|edge case|complexity|hitung|mengapa|kenapa|bandingkan|rancang'
    $hits = [regex]::Matches($text.ToLower(), $kw).Count
    if ($hits -gt 0) { $score += [math]::Min(4, 2 * $hits); [void]$reasons.Add("keywords:$hits") }
    $nums = [regex]::Matches($text, '\d+(\.\d+)?').Count
    if ($nums -ge 2 -and $text -match '\?|\bhow\b|\bwhat\b|berapa') { $score += 3; [void]$reasons.Add('word-problem') }
    elseif ($nums -ge 3 -or $text -match '[=^\u221A\u2211\u222B]|\d\s*[-+*/x%]\s*\d') { $score += 2; [void]$reasons.Add('math') }
    if ($text -match '(?m)^\s*\(?[A-D][\)\.]\s') { $score += 3; [void]$reasons.Add('multiple-choice') }
    if ($text -match '```|Traceback|Exception|\berror:|at .+:\d+') { $score += 2; [void]$reasons.Add('code/error') }
    if (([regex]::Matches($text, '\?').Count -gt 1) -or ($text -match '(?m)^\s*\d+[\.\)]\s')) { $score += 1; [void]$reasons.Add('multi-part') }
    if ($text.Length -gt 600) { $score += 1; [void]$reasons.Add('long') }
    if ($score -lt 3 -and $text -match '^(hi|hello|hey|halo|thanks|terima kasih)\b|translate|terjemah|summari[sz]e|ringkas|rewrite|\blist\b|what is|apa itu|\bdefine\b') { $score -= 3; [void]$reasons.Add('lookup/edit') }
    if ($HasSchema) { $score -= 2; [void]$reasons.Add('schema') }
    [pscustomobject]@{ Think = ($score -ge $Threshold); Score = $score; Reasons = @($reasons); Prompt = $text }
}

# Builds an OpenAI-style chat body. Thinking toggles Qwen's recommended sampling set.
function New-ChatBody {
    param([object[]]$Messages, [bool]$Think, [string]$SchemaFile = '', [string]$GrammarFile = '',
          [int]$Seed = -1, [int]$MaxTokens = -1, [double]$Temperature = -1)
    $body = [ordered]@{ messages = $Messages; stream = $false; chat_template_kwargs = @{ enable_thinking = $Think }; top_k = 20; min_p = 0 }
    if ($Think) { $body.temperature = 1.0; $body.top_p = 0.95 } else { $body.temperature = 0.7; $body.top_p = 0.8 }
    if ($Temperature -ge 0) { $body.temperature = $Temperature }
    if ($Seed -ge 0) { $body.seed = $Seed }
    if ($MaxTokens -gt 0) { $body.max_tokens = $MaxTokens }
    if ($SchemaFile) {
        $schema = Get-Content $SchemaFile -Raw | ConvertFrom-Json
        $body.response_format = @{ type = 'json_schema'; json_schema = @{ name = [IO.Path]::GetFileNameWithoutExtension($SchemaFile); strict = $true; schema = $schema } }
    }
    if ($GrammarFile) { $body.grammar = Get-Content $GrammarFile -Raw }
    $body
}

function Invoke-Chat($Body, [int]$Port = 8080) {
    $json = $Body | ConvertTo-Json -Depth 30
    Invoke-RestMethod "http://127.0.0.1:$Port/v1/chat/completions" -Method Post -ContentType 'application/json; charset=utf-8' `
        -Body ([Text.Encoding]::UTF8.GetBytes($json)) -TimeoutSec 7200
}

# Parses a reply as JSON and checks the schema's top-level required keys. Returns $null when invalid.
function Test-JsonReply([string]$Text, [string]$SchemaFile) {
    $clean = $Text.Trim() -replace '^```(json)?\s*', '' -replace '\s*```$', ''
    try { $obj = $clean | ConvertFrom-Json } catch { return $null }
    if ($SchemaFile) {
        $schema = Get-Content $SchemaFile -Raw | ConvertFrom-Json
        foreach ($k in @($schema.required)) { if ($k -and -not ($obj.PSObject.Properties.Name -contains $k)) { return $null } }
    }
    $obj
}

function Format-Timings($r) {
    $t = $r.timings
    $s = "[prompt {0} tok {1:N1}s | gen {2} tok {3:N2} tok/s" -f $t.prompt_n, ($t.prompt_ms / 1000), $t.predicted_n, $t.predicted_per_second
    if ($t.draft_n) { $s += " | draft accepted {0}/{1}" -f $t.draft_n_accepted, $t.draft_n }
    "$s]"
}

# One question end to end: think routing, optional RAG, optional schema (validated, retried once).
# -ThinkMode auto|on|off   -RagMode off|on|auto   Returns a result object (used by ask.ps1, chat.ps1 and eval.ps1).
function Invoke-Ask {
    param([string]$Prompt, [ValidateSet('auto','on','off')][string]$ThinkMode = 'auto',
          [ValidateSet('off','on','auto')][string]$RagMode = 'off', [double]$RagAutoMin = 4.0,
          [string]$SchemaFile = '', [string]$GrammarFile = '', [object[]]$History = @(),
          [int]$Seed = -1, [int]$MaxTokens = -1, $ServerProfile = $null)
    if (-not $ServerProfile) { $ServerProfile = Get-ServerProfile }
    $port = [int]$ServerProfile.Port
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $d = Get-ThinkDecision $Prompt ([int]$ServerProfile.ThinkThreshold) ([bool]$SchemaFile)
    if ($ThinkMode -eq 'on')  { $d.Think = $true;  $d.Reasons = @('forced on') }
    if ($ThinkMode -eq 'off') { $d.Think = $false; $d.Reasons = @('forced off') }
    $question = $d.Prompt

    $hits = @(); $content = $question
    if ($RagMode -ne 'off') {
        Import-Module "$LlmRoot\scripts\rag.psm1" -DisableNameChecking
        $hits = @(Search-Rag $question -K ([int]$ServerProfile.RagK) -MaxTokens ([int]$ServerProfile.RagTokens))
        if ($RagMode -eq 'auto' -and $hits.Count -and $hits[0].Score -lt $RagAutoMin) { $hits = @() }
        # Explicit RAG with no hits still uses the grounded template, so the model says "Not in my documents".
        if ($hits.Count -or $RagMode -eq 'on') { $content = Format-RagPrompt $question $hits }
    }
    $messages = @($History) + @(@{ role = 'user'; content = $content })
    $responses = New-Object System.Collections.ArrayList
    $reasoning = ''

    # Thinking + schema: think freely first, then format with the schema in a non-thinking pass.
    if ($d.Think -and ($SchemaFile -or $GrammarFile)) {
        $r1 = Invoke-Chat (New-ChatBody -Messages $messages -Think $true -Seed $Seed -MaxTokens $MaxTokens) $port
        [void]$responses.Add($r1); $reasoning = $r1.choices[0].message.reasoning_content
        $messages = @(@{ role = 'user'; content = "Request:`n$content`n`nDraft answer:`n$($r1.choices[0].message.content)`n`nReturn the final answer in the required format." })
        $think2 = $false
    } else { $think2 = $d.Think }

    $body = New-ChatBody -Messages $messages -Think $think2 -SchemaFile $SchemaFile -GrammarFile $GrammarFile -Seed $Seed -MaxTokens $MaxTokens
    $r = Invoke-Chat $body $port; [void]$responses.Add($r)
    if ($r.choices[0].message.reasoning_content) { $reasoning = $r.choices[0].message.reasoning_content }
    $answer = $r.choices[0].message.content
    $parsed = $null; $retries = 0
    if ($SchemaFile) {
        $parsed = Test-JsonReply $answer $SchemaFile
        if (-not $parsed) {
            $retries = 1; $body.temperature = 0
            $r = Invoke-Chat $body $port; [void]$responses.Add($r)
            $answer = $r.choices[0].message.content; $parsed = Test-JsonReply $answer $SchemaFile
        }
    }
    $sum = { param($name) ($responses | ForEach-Object { [double]$_.timings.$name } | Measure-Object -Sum).Sum }
    [pscustomobject]@{
        Content = $answer; Reasoning = $reasoning; Think = $d.Think; ThinkScore = $d.Score; ThinkReasons = $d.Reasons
        Sources = @($hits | ForEach-Object { $_.Chunk.id }); Json = $parsed; JsonValid = [bool]$parsed; Retries = $retries
        PromptTokens = & $sum 'prompt_n'; PromptMs = & $sum 'prompt_ms'; GenTokens = & $sum 'predicted_n'
        GenTps = $r.timings.predicted_per_second; DraftN = & $sum 'draft_n'; DraftAccepted = & $sum 'draft_n_accepted'
        Calls = $responses.Count; WallMs = $sw.ElapsedMilliseconds; Question = $question; Last = $r
    }
}
