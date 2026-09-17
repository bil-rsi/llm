# llama-bench sweep for a model on a backend. Every numeric option accepts a comma list (llama-bench runs the cross product).
# Output: C:\llm\logs\bench\<model>-<backend>-<tag>-<timestamp>.md  (old bench-*.txt files are the baseline)
# Examples:
#   bench.ps1 -Model 35b -Pp 512,2048 -Tg 32 -Ub 256,512,1024,2048 -Tag ub
#   bench.ps1 -Model 27b -Ctk f16,q8_0,q4_0 -Ctv f16,q8_0,q4_0 -Depth 0,8192 -Tg 32 -Reps 2 -Tag kv
#   bench.ps1 -Model 35b -Threads 2,4,6,8 -Tg 64 -Tag threads
param([ValidateSet('27b','35b')][string]$Model = '35b', [ValidateSet('cpu','vulkan')][string]$Backend = 'vulkan',
      [string]$Threads = '6', [string]$Ngl = '99', [string]$Pp = '512', [string]$Tg = '128', [int]$Reps = 3,
      [string]$Ctk = 'f16', [string]$Ctv = 'f16', [string]$Ub = '512', [string]$B = '2048', [string]$Depth = '0',
      [string]$Fa = '1', [string]$Tag = 'run', [string]$ModelFile = '', [string]$BuildDir = '')
$models = @{ '27b' = 'Qwen3.6-27B-Q4_K_M.gguf'; '35b' = 'Qwen3.6-35B-A3B-UD-Q4_K_M.gguf' }
$file = if ($ModelFile) { $ModelFile } else { "C:\llm\models\$($models[$Model])" }
$dir = if ($BuildDir) { $BuildDir } else { "C:\llm\llama.cpp\$Backend" }
New-Item -ItemType Directory -Force C:\llm\logs\bench | Out-Null
$out = "C:\llm\logs\bench\$Model-$Backend-$Tag-$(Get-Date -Format 'yyyyMMdd-HHmm').md"
& "$dir\llama-bench.exe" -m $file -t $Threads -ngl $Ngl -p $Pp -n $Tg -r $Reps -fa $Fa `
    -ctk $Ctk -ctv $Ctv -ub $Ub -b $B -d $Depth -o md | Tee-Object $out
Write-Host "Saved: $out"
