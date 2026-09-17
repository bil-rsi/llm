# Stops the llama-server started by start.ps1.
$pidFile = 'C:\llm\logs\server.pid'
$p = if (Test-Path $pidFile) { Get-Process -Id (Get-Content $pidFile) -ErrorAction SilentlyContinue }
if ($p -and $p.Name -eq 'llama-server') { Stop-Process -Id $p.Id -Force; Write-Host "Stopped llama-server (PID $($p.Id))" }
else { Write-Host 'No llama-server started by start.ps1 is running.' }
Remove-Item $pidFile, 'C:\llm\logs\server.profile.json' -ErrorAction SilentlyContinue
