# llama-bench sweep for a model on one or more backends. Every numeric option accepts a comma list (llama-bench runs the cross product).
# Output: C:\llm\logs\bench\<model>-<backend>-<tag>-<timestamp>.md, headed by build, driver, model size, env and arguments
# (old bench-*.txt files are the b11011 baseline).
# Examples:
#   bench.ps1 -Model 35b -Pp 512,2048 -Tg 32 -Ub 256,512,1024,2048 -Tag ub
#   bench.ps1 -Model 27b -Ctk f16,q8_0 -Ctv f16,q8_0,q4_0 -Depth 0,8192,16384 -Tg 32 -Reps 2 -Tag kv
#   bench.ps1 -Model 27b -Backend cpu,vulkan -Pp 512 -Tg 128 -Tag backend      (cpu runs use -ngl 0)
#   bench.ps1 -Model 35b -Env 'GGML_VK_DISABLE_F16=1' -Tag nof16               (env set for this run only)
param([ValidateSet('27b','35b')][string]$Model = '35b', [ValidatePattern('^(cpu|vulkan)(,(cpu|vulkan))*$')][string]$Backend = 'vulkan',
      [string]$Threads = '6', [string]$Ngl = '99', [string]$Pp = '512', [string]$Tg = '128', [int]$Reps = 3,
      [string]$Ctk = 'f16', [string]$Ctv = 'f16', [string]$Ub = '512', [string]$B = '2048', [string]$Depth = '0',
      [string]$Fa = '1', [string]$Tag = 'run', [string]$ModelFile = '', [string]$BuildDir = '', [Alias('Env')][string]$EnvVars = '')
$models = @{ '27b' = 'Qwen3.6-27B-Q4_K_M.gguf'; '35b' = 'Qwen3.6-35B-A3B-UD-Q4_K_M.gguf' }
$file = if ($ModelFile) { $ModelFile } else { "C:\llm\models\$($models[$Model])" }
. "$PSScriptRoot\lib.ps1"
$bad = Test-ModelFile $file; if ($bad) { throw $bad }
New-Item -ItemType Directory -Force C:\llm\logs\bench | Out-Null
$vars = @($EnvVars -split ',' | Where-Object { $_ -match '=' } | ForEach-Object { $k, $v = $_ -split '=', 2; [pscustomobject]@{ Name = $k.Trim(); Value = $v.Trim() } })
$driver = (Get-CimInstance Win32_VideoController | Where-Object Name -match 'Intel' | Select-Object -First 1).DriverVersion
foreach ($be in ($Backend -split ',')) {
    $dir = if ($BuildDir) { $BuildDir } else { "C:\llm\llama.cpp\$be" }
    $ngl = if ($be -eq 'cpu') { '0' } else { $Ngl }
    $out = "C:\llm\logs\bench\$Model-$be-$Tag-$(Get-Date -Format 'yyyyMMdd-HHmm').md"
    $build = (& { $ErrorActionPreference = 'Continue'; & "$dir\llama-bench.exe" --version 2>&1 | ForEach-Object { "$_" } }) -match 'version:|built with' -join ' '
    $cliArgs = @('-m', $file, '-t', $Threads, '-ngl', $ngl, '-p', $Pp, '-n', $Tg, '-r', $Reps, '-fa', $Fa, '-ctk', $Ctk, '-ctv', $Ctv, '-ub', $Ub, '-b', $B, '-d', $Depth, '-o', 'md')
    @("<!-- build: $build | driver: $driver | model: $(Split-Path $file -Leaf) $((Get-Item $file).Length) bytes -->",
      "<!-- env: $(if ($vars.Count) { ($vars | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ' ' } else { 'none' }) | args: $($cliArgs -join ' ') -->", '') | Set-Content $out -Encoding UTF8
    $old = @{}; foreach ($v in $vars) { $old[$v.Name] = [Environment]::GetEnvironmentVariable($v.Name); [Environment]::SetEnvironmentVariable($v.Name, $v.Value) }
    try { & "$dir\llama-bench.exe" @cliArgs | ForEach-Object { $_; Add-Content $out $_ -Encoding UTF8 } }
    finally { foreach ($v in $vars) { [Environment]::SetEnvironmentVariable($v.Name, $old[$v.Name]) } }
    Write-Host "Saved: $out"
}
