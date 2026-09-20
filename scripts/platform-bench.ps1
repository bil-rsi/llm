# Benchmarks the running platform: in-container stage timings (bench.py) + host-side HTTP latency through the published
# port (Windows -> WSL2 forwarding included). Results: platform\bench\results\bench-<timestamp>.json
# Usage: powershell -ExecutionPolicy Bypass -File C:\llm-platform\scripts\platform-bench.ps1 [-N 1000] [-Memories 1000] [-LlmRuns 5]
param([int]$N = 1000, [int]$Memories = 1000, [int]$LlmRuns = 5)
$ErrorActionPreference = 'Stop'
$plat = Join-Path (Split-Path $PSScriptRoot -Parent) 'platform'
function Invoke-Docker { $ErrorActionPreference = 'Continue'; & docker @args 2>&1 | ForEach-Object { "$_" }; $global:DockerExit = $LASTEXITCODE }
New-Item -ItemType Directory -Force "$plat\bench\results" | Out-Null
$stamp = Get-Date -Format yyyyMMdd-HHmmss
Push-Location $plat
try {
    $raw = Get-Content "$plat\bench\bench.py" -Raw
    $psi = New-Object Diagnostics.ProcessStartInfo 'docker', "compose exec -T -e BENCH_N=$N -e BENCH_MEMORIES=$Memories -e BENCH_LLM=$LlmRuns backend python -"
    $psi.RedirectStandardInput = $true; $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true; $psi.UseShellExecute = $false; $psi.WorkingDirectory = $plat
    $p = [Diagnostics.Process]::Start($psi); $p.StandardInput.Write($raw); $p.StandardInput.Close()
    $out = $p.StandardOutput.ReadToEnd(); $err = $p.StandardError.ReadToEnd(); $p.WaitForExit()
    if ($p.ExitCode) { throw "bench.py failed:`n$err" }
    $inside = $out | ConvertFrom-Json
    # Host-side: curl.exe through 127.0.0.1:8090 (includes Windows->WSL2 port forwarding)
    $curl = (Get-Command curl.exe).Source; $times = New-Object System.Collections.ArrayList
    for ($i = 0; $i -lt [math]::Min($N, 300); $i++) { [void]$times.Add([double](& $curl -s -o NUL -w '%{time_total}' http://127.0.0.1:8090/health/live) * 1000) }
    $s = $times | Sort-Object; $c = $s.Count
    $hostHttp = [ordered]@{ n = $c; p50 = [math]::Round($s[[int]($c / 2)], 3); p95 = [math]::Round($s[[int]($c * 0.95)], 3); p99 = [math]::Round($s[[math]::Min($c - 1, [int]($c * 0.99))], 3); note = 'includes curl.exe process start-up' }
    $result = [ordered]@{ timestamp = $stamp; host = [ordered]@{ http_health_live_curl = $hostHttp }; container = $inside }
    $file = "$plat\bench\results\bench-$stamp.json"
    $result | ConvertTo-Json -Depth 8 | Set-Content $file -Encoding UTF8
    Write-Host "Results: $file"
} finally { Pop-Location }
