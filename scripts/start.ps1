# Starts llama-server in the background using a named profile (speed | accuracy | deep).
# Usage: powershell -ExecutionPolicy Bypass -File C:\llm\scripts\start.ps1 [-Profile speed] [-Model 27b|35b] [-Think]
#        [-Ctx N] [-Ub N] [-Batch N] [-Ctk f16|q8_0|q4_0] [-Ctv ...] [-Spec none|ngram-mod|ngram-cache|draft-simple]
#        [-Draft file.gguf] [-DraftMax N] [-ReasoningBudget N] [-Backend cpu|vulkan] [-BuildDir dir] [-Threads N] [-Ngl N]
# Explicit parameters override the profile. Reasoning is 'auto' by default so clients can switch it per request
# (chat_template_kwargs.enable_thinking); -Think forces it on for every request.
param(
    [ValidateSet('speed', 'accuracy', 'deep')][string]$Profile = 'speed',
    [ValidateSet('', '27b', '35b')][string]$Model = '',
    [ValidateSet('', 'on', 'off', 'auto')][string]$Reasoning = '',
    [switch]$Think,
    [int]$Ctx = 0, [int]$Ub = 0, [int]$Batch = 0,
    [ValidateSet('', 'f16', 'q8_0', 'q5_1', 'q5_0', 'q4_1', 'q4_0', 'iq4_nl')][string]$Ctk = '',
    [ValidateSet('', 'f16', 'q8_0', 'q5_1', 'q5_0', 'q4_1', 'q4_0', 'iq4_nl')][string]$Ctv = '',
    [ValidateSet('', 'none', 'ngram-mod', 'ngram-cache', 'ngram-simple', 'draft-simple', 'draft-mtp')][string]$Spec = '',
    [string]$Draft = '', [int]$DraftMax = 3,
    [int]$ReasoningBudget = -2,
    [ValidateSet('', 'cpu', 'vulkan')][string]$Backend = '', [string]$BuildDir = '',
    [int]$Threads = 0, [int]$Ngl = -1, [int]$Port = 8080
)
$ErrorActionPreference = 'Stop'
$root = 'C:\llm'

$models = @{
    '27b' = @{ File = 'Qwen3.6-27B-Q4_K_M.gguf';        Alias = 'qwen3.6-27b' }
    '35b' = @{ File = 'Qwen3.6-35B-A3B-UD-Q4_K_M.gguf'; Alias = 'qwen3.6-35b-a3b' }
}
# Ub/Threads/Spec are provisional until the bench sweeps in logs\bench\ confirm them.
# Client-side keys (ThinkThreshold, RagK, RagTokens) are read by lib.ps1 via logs\server.profile.json.
$profiles = @{
    speed    = @{ Model = '35b'; Ctx = 16384; Ub = 1024; Batch = 2048; Ctk = 'f16';  Ctv = 'f16';  Spec = 'none'; Budget = 512
                  ThinkThreshold = 4; RagK = 2; RagTokens = 800 }
    accuracy = @{ Model = '35b'; Ctx = 32768; Ub = 1024; Batch = 2048; Ctk = 'f16';  Ctv = 'f16';  Spec = 'none'; Budget = 3072
                  ThinkThreshold = 3; RagK = 4; RagTokens = 2000 }
    deep     = @{ Model = '27b'; Ctx = 16384; Ub = 512;  Batch = 2048; Ctk = 'q8_0'; Ctv = 'q8_0'; Spec = 'none'; Budget = 4096
                  ThinkThreshold = 3; RagK = 3; RagTokens = 1200 }
}
$p = $profiles[$Profile]
if (-not $Model)   { $Model = $p.Model }
if (-not $Backend) { $Backend = 'vulkan' }
if ($Threads -le 0) { $Threads = 6 }
if ($Ngl -lt 0)    { $Ngl = if ($Backend -eq 'cpu') { 0 } else { 99 } }
if ($Ctx -le 0)    { $Ctx = $p.Ctx }
if ($Ub -le 0)     { $Ub = $p.Ub }
if ($Batch -le 0)  { $Batch = $p.Batch }
if (-not $Ctk)     { $Ctk = $p.Ctk }
if (-not $Ctv)     { $Ctv = $p.Ctv }
if (-not $Spec)    { $Spec = if ($Draft) { 'draft-simple' } else { $p.Spec } }
if ($ReasoningBudget -lt -1) { $ReasoningBudget = $p.Budget }
if ($Think)        { $Reasoning = 'on' }
if (-not $Reasoning) { $Reasoning = 'auto' }
if (-not $BuildDir) { $BuildDir = "$root\llama.cpp\$Backend" }
$m = $models[$Model]
$modelPath = "$root\models\$($m.File)"

$pidFile = "$root\logs\server.pid"
if (Test-Path $pidFile) {
    $old = Get-Process -Id (Get-Content $pidFile) -ErrorAction SilentlyContinue
    if ($old -and $old.Name -eq 'llama-server') { throw "llama-server already running (PID $($old.Id)). Run stop.ps1 first." }
}
foreach ($f in @($modelPath, "$BuildDir\llama-server.exe") + @($Draft | Where-Object { $_ })) {
    if (-not (Test-Path $f)) { throw "Not found: $f" }
}

# Server-level sampling is the Qwen non-thinking set; clients send the thinking set per request.
if ($Reasoning -eq 'on') { $sampling = @('--temp', '1.0', '--top-p', '0.95', '--top-k', '20', '--min-p', '0') }
else                     { $sampling = @('--temp', '0.7', '--top-p', '0.8', '--top-k', '20', '--min-p', '0') }

$cliArgs = @('-m', $modelPath, '-a', $m.Alias,
          '--host', '127.0.0.1', '--port', $Port,
          '-c', $Ctx, '-np', 1, '-t', $Threads, '-ngl', $Ngl, '-fa', 'auto', '--jinja',
          '-b', $Batch, '-ub', $Ub, '-ctk', $Ctk, '-ctv', $Ctv, '-ctxcp', 8,
          '--reasoning', $Reasoning, '--reasoning-budget', $ReasoningBudget,
          '--reasoning-budget-message', '"Thinking budget reached; give the final answer now."',
          '--cors-origins', 'localhost', '--no-cors-credentials', '-cram', 1024, '-lv', 4) + $sampling
if ($Spec -ne 'none') { $cliArgs += @('--spec-type', $Spec) }
if ($Draft) { $cliArgs += @('-md', $Draft, '-ngld', 99, '--spec-draft-n-max', $DraftMax, '-ctkd', 'q8_0', '-ctvd', 'q8_0') }

$log = "$root\logs\server-$Profile-$Model.log"
$proc = Start-Process -FilePath "$BuildDir\llama-server.exe" -ArgumentList $cliArgs -WindowStyle Hidden -PassThru `
    -RedirectStandardError $log -RedirectStandardOutput "$root\logs\server-$Profile-$Model.out.log"
$proc.Id | Set-Content $pidFile
@{ Profile = $Profile; Model = $Model; Alias = $m.Alias; Port = $Port; Reasoning = $Reasoning; Budget = $ReasoningBudget
   Ctx = $Ctx; Ub = $Ub; Ctk = $Ctk; Ctv = $Ctv; Spec = $Spec; Draft = $Draft; Build = $BuildDir
   ThinkThreshold = $p.ThinkThreshold; RagK = $p.RagK; RagTokens = $p.RagTokens } |
    ConvertTo-Json | Set-Content "$root\logs\server.profile.json" -Encoding UTF8
Write-Host ("Starting {0} [profile={1}, {2}, t={3}, ngl={4}, ctx={5}, ub={6}, kv={7}/{8}, spec={9}, reasoning={10}, budget={11}] PID {12}" -f `
    $m.Alias, $Profile, $Backend, $Threads, $Ngl, $Ctx, $Ub, $Ctk, $Ctv, $Spec, $Reasoning, $ReasoningBudget, $proc.Id)
Write-Host "Log: $log"

for ($i = 0; $i -lt 600; $i++) {
    Start-Sleep 1
    if ($proc.HasExited) { throw "llama-server exited early; see $log" }
    try { if ((Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 2).status -eq 'ok') { break } } catch {}
}
Write-Host "Ready: web UI http://127.0.0.1:$Port  |  API http://127.0.0.1:$Port/v1"
