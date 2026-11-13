# Start, stop or check the GPU model server AND the ngrok tunnel, with one command.
#
#   .\gpu_server.ps1 start     start the model server, then the ngrok tunnel
#   .\gpu_server.ps1 stop      stop both
#   .\gpu_server.ps1 status    what is running, GPU temperature/memory, health
#
# Run from the model_server folder. Settings come from model_server\.env:
#   GATEWAY_PORT (default 80)   NGROK_URL (the tunnel's address)
# The server stops itself if the GPU gets too hot or too full (see run_all.py).

param([ValidateSet("start", "stop", "status")][string]$Action = "start")

$Dir = $PSScriptRoot
$Logs = Join-Path $Dir "logs"
New-Item -ItemType Directory -Force $Logs | Out-Null

function Get-Setting($name, $default) {
    $line = Get-Content (Join-Path $Dir ".env") -ErrorAction SilentlyContinue | Where-Object { $_ -match "^$name=" } | Select-Object -First 1
    if ($line) { return ($line -split "=", 2)[1].Trim() } else { return $default }
}

$Port = Get-Setting "GATEWAY_PORT" "80"
$NgrokUrl = Get-Setting "NGROK_URL" "https://compel-dissuade-subheader.ngrok-free.dev"
$Local = "http://127.0.0.1:$Port/health"

function Find-Procs($pattern) {
    Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -match $pattern -and $_.ProcessId -ne $PID }
}
function Get-Health($url) {
    try {
        $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 8 -Headers @{ "ngrok-skip-browser-warning" = "1" }
        return ($r.Content | ConvertFrom-Json)
    } catch { return $null }
}
function Show-Gpu {
    $g = nvidia-smi --query-gpu=temperature.gpu,memory.used,memory.total,power.draw --format=csv,noheader 2>$null
    if ($g) { Write-Host "GPU (temp C, memory used, total, watts): $g" }
}
function Show-Health($label, $url) {
    $h = Get-Health $url
    if ($h) { Write-Host ("{0,-10} {1}  (llm {2}, stt {3}, tts {4})" -f $label, $h.status.ToUpper(), $h.llm, $h.stt, $h.tts) -ForegroundColor Green }
    else { Write-Host ("{0,-10} not answering" -f $label) -ForegroundColor Yellow }
}

function Stop-All {
    foreach ($p in (Find-Procs "run_all\.py")) { taskkill /PID $p.ProcessId /T /F | Out-Null; Write-Host "stopped model server (PID $($p.ProcessId))" }
    foreach ($p in (Find-Procs "llama-server|uvicorn (stt_server|tts_server|gateway)")) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    foreach ($p in (Find-Procs "ngrok.*http")) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host "stopped ngrok tunnel (PID $($p.ProcessId))" }
}

switch ($Action) {
    "status" {
        $srv = Find-Procs "run_all\.py"; $tun = Find-Procs "ngrok.*http"
        Write-Host ("model server : " + $(if ($srv) { "running (PID $($srv[0].ProcessId))" } else { "stopped" }))
        Write-Host ("ngrok tunnel : " + $(if ($tun) { "running -> $NgrokUrl (port $Port)" } else { "stopped" }))
        Show-Gpu
        Show-Health "local" $Local
        Show-Health "public" "$NgrokUrl/health"
    }

    "stop" { Stop-All; Start-Sleep 2; Show-Gpu }

    "start" {
        if (-not (Find-Procs "run_all\.py")) {
            Write-Host "Starting the model server (LLM first, then speech-to-text, voice, gateway)..."
            $p = Start-Process python -ArgumentList "-u", "run_all.py" -WorkingDirectory $Dir -WindowStyle Hidden -PassThru `
                -RedirectStandardOutput (Join-Path $Logs "run_all.out.log") -RedirectStandardError (Join-Path $Logs "run_all.err.log")
            $p.Id | Out-File (Join-Path $Logs "run_all.pid") -Encoding ascii
        } else { Write-Host "Model server is already running." }

        $deadline = (Get-Date).AddSeconds(240)
        $up = $false
        while ((Get-Date) -lt $deadline) {
            if (-not (Find-Procs "run_all\.py")) {
                Write-Host "The model server stopped by itself. Last lines of the log:" -ForegroundColor Red
                Get-Content (Join-Path $Logs "run_all.out.log") -Tail 5
                exit 1
            }
            $h = Get-Health $Local
            if ($h -and $h.status -eq "ok") { $up = $true; break }
            Start-Sleep 5
        }
        if (-not $up) { Write-Host "Not ready after 240 s. See model_server\logs\run_all.out.log" -ForegroundColor Red; exit 1 }
        Show-Health "local" $Local; Show-Gpu

        if (-not (Find-Procs "ngrok.*http")) {
            Write-Host "Opening the ngrok tunnel: ngrok http $Port --url $NgrokUrl"
            Start-Process ngrok -ArgumentList "http", $Port, "--url", $NgrokUrl -WindowStyle Hidden `
                -RedirectStandardOutput (Join-Path $Logs "ngrok.out.log") -RedirectStandardError (Join-Path $Logs "ngrok.err.log") | Out-Null
        } else { Write-Host "ngrok tunnel is already running." }

        $ok = $false
        foreach ($i in 1..12) { Start-Sleep 3; if (Get-Health "$NgrokUrl/health") { $ok = $true; break } }
        Show-Health "public" "$NgrokUrl/health"
        if (-not $ok) { Write-Host "Tunnel not answering yet. Check model_server\logs\ngrok.err.log (is another PC using this ngrok address?)" -ForegroundColor Yellow }
        else { Write-Host "`nAll set. Stop everything with: .\gpu_server.ps1 stop" }
    }
}
