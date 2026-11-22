# Set up the local chat models for Quorum on Windows (repeatable; safe to re-run).
#   powershell -ExecutionPolicy Bypass -File scripts\setup-local-models.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\setup-local-models.ps1 -Models gemma4:e4b,qwen3:8b
# Needs internet for the downloads only. Gemma 4 E4B is ~9.6 GB.
param([string[]]$Models = @("gemma4:e4b"))
$ErrorActionPreference = "Stop"
$MinVersion = [version]"0.31.0"   # gemma4 needs Ollama >= 0.31
$Url = if ($env:OLLAMA_HOST) { if ($env:OLLAMA_HOST -like "http*") { $env:OLLAMA_HOST } else { "http://$($env:OLLAMA_HOST)" } } else { "http://127.0.0.1:11434" }

function Test-Ollama { try { Invoke-RestMethod "$Url/api/version" -TimeoutSec 3 | Out-Null; $true } catch { $false } }

Write-Host "== 1/4 Ollama installed?"
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
  Write-Host "x Ollama is not installed. Install it with: winget install Ollama.Ollama  (or https://ollama.com/download)"; exit 1
}
$raw = (& ollama --version) -join " "
$version = [version]([regex]::Match($raw, "\d+\.\d+\.\d+").Value)
Write-Host "ok ollama $version"
if ($version -lt $MinVersion) {
  Write-Host "x Gemma 4 needs Ollama $MinVersion or newer (you have $version). Update it: winget upgrade Ollama.Ollama"; exit 1
}

Write-Host "== 2/4 Ollama running?"
if (-not (Test-Ollama)) {
  Write-Host "  Starting 'ollama serve' in the background..."
  $env:OLLAMA_FLASH_ATTENTION = "1"; $env:OLLAMA_KV_CACHE_TYPE = "q8_0"
  if (-not $env:OLLAMA_CONTEXT_LENGTH) { $env:OLLAMA_CONTEXT_LENGTH = "32768" }
  Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden
  for ($i = 0; $i -lt 30 -and -not (Test-Ollama); $i++) { Start-Sleep -Seconds 1 }
}
if (-not (Test-Ollama)) { Write-Host "x Ollama did not start."; exit 1 }
Write-Host "ok Ollama is running at $Url"

Write-Host "== 3/4 Downloading models (progress is shown; there is no time limit)"
$installed = (& ollama list) | Select-Object -Skip 1 | ForEach-Object { ($_ -split "\s+")[0] }
foreach ($model in $Models) {
  if ($installed -contains $model) { Write-Host "ok $model already downloaded"; continue }
  if ($model -eq "gemma4:e4b") { Write-Host "  $model is about 9.6 GB - this can take a while." }
  & ollama pull $model   # resumes if interrupted; just run the script again
  if ($LASTEXITCODE -ne 0) { Write-Host "x download of $model failed"; exit 1 }
}

Write-Host "== 4/4 Verifying"
& ollama list
foreach ($model in $Models) {
  $body = @{ model = $model; stream = $false; think = $false; messages = @(@{ role = "user"; content = "Reply with exactly: ready" }) } | ConvertTo-Json -Depth 4
  $reply = Invoke-RestMethod "$Url/api/chat" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 600
  Write-Host "  $model -> $($reply.message.content.Trim())"
}
Write-Host "ok Local models are ready. Select them in Quorum's sidebar (Local group)."
