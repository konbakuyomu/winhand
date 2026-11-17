# End-to-end check of the one-line install, on a Windows runner (desktop.yml, after the build):
# a local relay (wrangler dev) -> pair a computer from the home page -> run the command the page
# shows (with the installer just built) -> installed, autostart entry, config.toml -> the installed
# backend connects and the relay reports the computer online.
# The tray app is not started (WINHAND_NO_START): a CI runner has no desktop session to show it in.
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$relayDir = Join-Path $root 'relay'
$base = 'http://127.0.0.1:8787'
$logs = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { $env:TEMP }
$dev = $null; $agent = $null

function Wait-Until([scriptblock]$test, [int]$seconds, [string]$what) {
  for ($i = 0; $i -lt $seconds; $i++) {
    try { if (& $test) { return } } catch {}
    Start-Sleep -Seconds 1
  }
  throw "timed out waiting for $what"
}

try {
  Push-Location $relayDir
  npm ci --no-audit --no-fund
  if ($LASTEXITCODE -ne 0) { throw 'npm ci failed' }
  $passphrase = -join ((48..57) + (97..122) | Get-Random -Count 36 | ForEach-Object { [char]$_ })
  [IO.File]::WriteAllText((Join-Path $relayDir '.dev.vars'), "OWNER_PASSPHRASE=$passphrase`n")
  $env:WRANGLER_SEND_METRICS = 'false'
  $dev = Start-Process -FilePath 'npx.cmd' -ArgumentList 'wrangler', 'dev', '--ip', '127.0.0.1', '--port', '8787' `
    -RedirectStandardOutput (Join-Path $logs 'wrangler.out') -RedirectStandardError (Join-Path $logs 'wrangler.err') -PassThru -WindowStyle Hidden
  Pop-Location
  Wait-Until { (Invoke-WebRequest "$base/status.json" -UseBasicParsing).StatusCode -eq 200 } 120 'the local relay'

  $page = Invoke-WebRequest -Method Post -Uri "$base/pair" -Body @{ name = 'ci'; passphrase = $passphrase } -UseBasicParsing
  if ($page.Content -notmatch 'irm (http://127\.0\.0\.1:8787/i/[A-Za-z0-9_-]{22}) \| iex') { throw 'the home page shows no install command' }
  $command = $Matches[1]
  Write-Host "install command: irm $command | iex"

  $env:WINHAND_INSTALLER = (Get-ChildItem (Join-Path $root 'desktop\out') -Recurse -Filter 'winhand-*-Setup.exe' | Select-Object -First 1).FullName
  if (-not $env:WINHAND_INSTALLER) { throw 'no installer was built' }
  $env:WINHAND_NO_START = '1'
  # exactly what a person runs, in Windows PowerShell 5.1 (what a fresh Windows has)
  & powershell.exe -NoProfile -Command "irm $command | iex"

  $key = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{BD773DCA-E6D2-427C-92E1-5900C6FF13F1}_is1'
  $dir = $key.InstallLocation.TrimEnd('\')
  $backend = Join-Path $dir 'current\backend\winhand.exe'
  if (-not (Test-Path $backend)) { throw "not installed: $backend" }
  $autostart = (Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run').winhand
  if ($autostart -ne ('"' + (Join-Path $dir 'current\WinhandDesktop.exe') + '" --background')) { throw "autostart entry: $autostart" }
  $config = Get-Content (Join-Path $env:USERPROFILE '.winhand\config.toml') -Raw
  if ($config -notmatch 'url = "ws://127\.0\.0\.1:8787/pc/ci/agent"') { throw "config.toml has no relay address for ci: $config" }
  if ($config -notmatch 'device_token = "[A-Za-z0-9_-]{43}"') { throw 'config.toml has no device token' }
  if ((Invoke-WebRequest -Method Post -Uri $command -UseBasicParsing -SkipHttpErrorCheck).StatusCode -ne 410) { throw 'the install command worked twice' }

  $agent = Start-Process -FilePath $backend -ArgumentList 'connect' -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logs 'agent.out') -RedirectStandardError (Join-Path $logs 'agent.err')
  Wait-Until { (Invoke-RestMethod "$base/pc/ci/status.json").online } 60 'the installed winhand to connect'
  Write-Host 'PASS one-line install: paired, installed, autostart, config.toml, connected as /pc/ci'
} catch {
  foreach ($f in 'wrangler.err', 'agent.err') { $p = Join-Path $logs $f; if (Test-Path $p) { Write-Host "--- $f"; Get-Content $p -Tail 40 } }
  throw
} finally {
  foreach ($p in @($agent, $dev)) { if ($p -and -not $p.HasExited) { & taskkill.exe /PID $p.Id /T /F | Out-Null } }
  Remove-Item -LiteralPath (Join-Path $relayDir '.dev.vars') -ErrorAction SilentlyContinue
}
