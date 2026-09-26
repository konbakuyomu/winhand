<#
.SYNOPSIS
  Build the winhand tray app with its bundled backend: no Python or .NET needed on the target.

  1. agent -> backend\winhand.exe  (PyInstaller onedir, smoke-tested with Python removed from PATH)
  2. desktop\windows -> self-contained WinUI 3 app
  3. backend copied next to the app: <out>\app\backend\winhand.exe
  4. optional: -Zip writes a portable archive, -Installer builds a Velopack Setup.exe

.EXAMPLE
  ./desktop/scripts/Build-Windows.ps1 -Zip
#>
[CmdletBinding()]
param(
    [ValidateSet('x64', 'arm64')] [string] $Architecture = 'x64',
    [string] $Output = (Join-Path $PSScriptRoot '..\out'),
    [switch] $SkipSmoke,
    [switch] $Zip,
    [switch] $Installer
)
$ErrorActionPreference = 'Stop'

# Windows PowerShell 5.1 turns any stderr line of a native command into a terminating error
# under 'Stop' (uv prints warnings there). Run tools with 'Continue' and check exit codes instead.
function Invoke-Native([string] $What, [scriptblock] $Command) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Command 2>&1 | ForEach-Object { "$_" } }
    finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit $LASTEXITCODE)" }
}
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$agent = Join-Path $repo 'agent'
$project = Join-Path $repo 'desktop\windows\Winhand.Desktop.csproj'
$Output = [IO.Path]::GetFullPath($Output)
$app = Join-Path $Output 'app'
$version = (Select-String -Path (Join-Path $agent 'pyproject.toml') -Pattern '^version = "([^"]+)"').Matches[0].Groups[1].Value

Write-Host "== winhand $version ($Architecture) -> $app"
if (Test-Path -LiteralPath $app) { Remove-Item -LiteralPath $app -Recurse -Force }
New-Item -ItemType Directory -Force -Path $app | Out-Null

# 1. backend. A separate environment: the .venv may be in use by a running `winhand connect`.
Push-Location $agent
try {
    $env:UV_PROJECT_ENVIRONMENT = '.venv-build'
    Remove-Item Env:VIRTUAL_ENV -ErrorAction SilentlyContinue  # set when launched from an activated venv
    Invoke-Native 'uv sync' { uv sync --locked --group build }
    $result = Join-Path $Output 'backend-build.json'
    $arguments = @('run', '--group', 'build', 'python', 'scripts/build_binary.py', '--result-file', $result)
    if (-not $SkipSmoke) { $arguments += '--smoke' }
    Invoke-Native 'backend build' { uv @arguments } | Where-Object { $_ -notmatch '^\d+ (INFO|WARNING)' }
    $bundle = (Get-Content -Raw -LiteralPath $result | ConvertFrom-Json).bundle
}
finally {
    Remove-Item Env:UV_PROJECT_ENVIRONMENT -ErrorAction SilentlyContinue
    Pop-Location
}

# 2. app
$platform = if ($Architecture -eq 'arm64') { 'ARM64' } else { 'x64' }
Invoke-Native 'dotnet publish' {
    dotnet publish $project -c Release -r "win-$Architecture" --self-contained true -p:Platform=$platform -p:Version=$version -o $app -nologo -v q
}

# 3. backend next to the app
Copy-Item -LiteralPath $bundle -Destination (Join-Path $app 'backend') -Recurse
foreach ($required in @('WinhandDesktop.exe', 'backend\winhand.exe', 'Assets\winhand.ico', 'Assets\winhand-offline.ico', 'Assets\mascot.png')) {
    if (-not (Test-Path -LiteralPath (Join-Path $app $required))) { throw "missing from the build: $required" }
}
Write-Host "== app ready: $app"

# 4. distribution
if ($Zip) {
    $archive = Join-Path $Output "winhand-$version-win-$Architecture.zip"
    if (Test-Path -LiteralPath $archive) { Remove-Item -LiteralPath $archive }
    Compress-Archive -Path (Join-Path $app '*') -DestinationPath $archive
    Write-Host "== portable zip: $archive"
}
if ($Installer) {
    if (-not (Get-Command vpk -ErrorAction SilentlyContinue)) { Invoke-Native 'vpk install' { dotnet tool install -g vpk } }
    Invoke-Native 'vpk pack' {
        vpk pack --packId winhand --packVersion $version --packDir $app --mainExe WinhandDesktop.exe `
            --packTitle winhand --icon (Join-Path $repo 'desktop\windows\Assets\winhand.ico') `
            --runtime "win-$Architecture" --outputDir (Join-Path $Output 'releases')
    }
    Write-Host "== installer: $(Join-Path $Output 'releases')"
}
