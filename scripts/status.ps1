# Shows llama-server memory, CPU and iGPU usage.
$p = Get-Process llama-server -ErrorAction SilentlyContinue
if (-not $p) { Write-Host 'llama-server not running'; return }
$pf = 'C:\llm\logs\server.profile.json'
if (Test-Path $pf) { $c0 = Get-Content $pf -Raw | ConvertFrom-Json; "Profile {0}: {1} ctx={2} ub={3} kv={4}/{5} spec={6} reasoning={7} budget={8}" -f $c0.Profile, $c0.Alias, $c0.Ctx, $c0.Ub, $c0.Ctk, $c0.Ctv, $c0.Spec, $c0.Reasoning, $c0.Budget }
$os = Get-CimInstance Win32_OperatingSystem
"PID {0}  RAM working set {1:N2} GB  private {2:N2} GB  |  system free RAM {3:N1} GB" -f $p.Id, ($p.WorkingSet64/1GB), ($p.PrivateMemorySize64/1GB), ($os.FreePhysicalMemory/1MB)
$c = Get-Counter '\Processor(_Total)\% Processor Time', '\GPU Engine(*engtype_3D)\Utilization Percentage', '\GPU Adapter Memory(*)\Shared Usage', '\GPU Adapter Memory(*)\Dedicated Usage' -ErrorAction SilentlyContinue
"CPU total: {0:N0}%" -f ($c.CounterSamples | ? Path -like '*processor(_total)*').CookedValue
"iGPU 3D utilisation: {0:N0}%" -f (($c.CounterSamples | ? Path -like '*engtype_3d*' | Measure-Object CookedValue -Sum).Sum)
"iGPU shared memory in use: {0:N2} GB" -f ((($c.CounterSamples | ? Path -like '*shared usage*' | Measure-Object CookedValue -Sum).Sum)/1GB)
