# Replace the VPS .env with your local .env over SSH (backs up the old one first).
#
#   .\scripts\push_env.ps1 -Key C:\Users\moham\Downloads\ssh.pem
#   .\scripts\push_env.ps1 -Key C:\path\key.pem -Deploy      # also runs scripts/deploy.sh
#
# Defaults: ubuntu@industinternational.com, repo in ~/kodera on the server.
param(
    [Parameter(Mandatory = $true)][string]$Key,
    [string]$Server = "industinternational.com",
    [string]$User = "ubuntu",
    [string]$Dir = "~/kodera",
    [switch]$Deploy
)
$ErrorActionPreference = "Stop"
$env_file = Join-Path $PSScriptRoot "..\.env"
if (-not (Test-Path $env_file)) { throw ".env not found at $env_file" }
if (-not (Test-Path $Key)) { throw "SSH key not found: $Key" }

$target = "$User@$Server"
$ssh = @("-i", $Key, "-o", "IdentitiesOnly=yes")

Write-Host "==> Uploading .env to $target"
scp @ssh $env_file "${target}:/tmp/kodera.env.new"
if ($LASTEXITCODE -ne 0) { throw "scp failed" }

$remote = "cd $Dir && { [ -f .env ] && cp .env .env.bak.`$(date +%F-%H%M%S) || true; } && mv /tmp/kodera.env.new .env && chmod 600 .env && echo 'remote .env replaced (old one saved as .env.bak.*)'"
if ($Deploy) { $remote += " && bash scripts/deploy.sh" }

Write-Host "==> Installing on the server"
ssh @ssh $target $remote
if ($LASTEXITCODE -ne 0) { throw "remote step failed" }
Write-Host "Done."
