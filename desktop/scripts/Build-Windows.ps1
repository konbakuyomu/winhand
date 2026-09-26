<#
.SYNOPSIS
  Build the winhand tray app with its bundled backend: no Python or .NET needed on the target.

  1. agent -> backend\winhand.exe  (PyInstaller onedir, smoke-tested with Python removed from PATH)
  2. desktop\windows -> self-contained WinUI 3 app
  3. backend copied next to the app: <out>\app\backend\winhand.exe
  4. -SigningMode Required: Authenticode-sign our own files with the pinned identity
     (desktop/packaging/windows/winhand.cer) and verify signature, digest and timestamp
  5. optional: -Zip writes a portable archive, -Installer builds the Velopack Setup.exe and
     update packages (a delta too when the previous release sits in <out>\releases)

.EXAMPLE
  ./desktop/scripts/Build-Windows.ps1 -Zip
  ./desktop/scripts/Build-Windows.ps1 -Installer -SigningMode Required
#>
[CmdletBinding()]
param(
    [ValidateSet('x64', 'arm64')] [string] $Architecture = 'x64',
    [string] $Output = (Join-Path $PSScriptRoot '..\out'),
    [ValidateSet('Skip', 'Required')] [string] $SigningMode = 'Skip',
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
$signed = $SigningMode -eq 'Required'
$signatures = @()
if ($signed) {
    if ($PSVersionTable.PSVersion.Major -lt 7) { throw 'Signed builds need PowerShell 7 (pwsh).' }
    . (Join-Path $PSScriptRoot 'Windows-Signing.ps1')
    $certificatePath = Join-Path $repo 'desktop\packaging\windows\winhand.cer'
    $expected = Get-ExpectedSigningCertificate $certificatePath
    # fail before a long build if the private key is not available
    if (-not (Test-Path -LiteralPath "Cert:/CurrentUser/My/$($expected.Thumbprint)")) {
        throw 'Signing identity not found in CurrentUser\My; run Import-WindowsSigningIdentity.ps1 (CI) or restore it locally.'
    }
}

Write-Host "== winhand $version ($Architecture, signing: $SigningMode) -> $app"
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

# 4. sign what we build; third-party files (Windows App SDK, .NET, Python) keep their original bytes
if ($signed) {
    foreach ($owned in @('WinhandDesktop.exe', 'WinhandDesktop.dll', 'backend\winhand.exe')) {
        $signatures += Invoke-WindowsSigning (Join-Path $app $owned) $expected
        Write-Host "   signed $owned"
    }
    $check = & (Join-Path $app 'backend\winhand.exe') --version
    if ($LASTEXITCODE -ne 0 -or "$check" -ne "winhand $version") { throw "signed backend does not run: $check" }
}
Write-Host "== app ready: $app"

# 5. distribution
if ($Zip) {
    $archive = Join-Path $Output "winhand-$version-win-$Architecture.zip"
    if (Test-Path -LiteralPath $archive) { Remove-Item -LiteralPath $archive }
    Compress-Archive -Path (Join-Path $app '*') -DestinationPath $archive
    Write-Host "== portable zip: $archive"
}
if ($Installer) {
    if (-not (Get-Command vpk -ErrorAction SilentlyContinue)) { Invoke-Native 'vpk install' { dotnet tool install -g vpk --version 1.2.0 } }
    $releases = Join-Path $Output 'releases'
    $pack = @('pack', '--packId', 'winhand', '--packVersion', $version, '--packDir', $app,
        '--mainExe', 'WinhandDesktop.exe', '--packTitle', 'winhand', '--packAuthors', 'konbakuyomu',
        '--icon', (Join-Path $repo 'desktop\windows\Assets\winhand.ico'),
        '--runtime', "win-$Architecture", '--outputDir', $releases)
    if ($signed) {
        # Our files are signed above. vpk adds Update.exe (as Squirrel.exe), an execution stub and
        # Setup.exe; sign exactly those, leave every other dependency untouched.
        $template = '"{0}" -NoProfile -File "{1}" -VelopackHelper -Path {{{{file}}}}' -f
            (Get-Process -Id $PID).Path, (Join-Path $PSScriptRoot 'Sign-WindowsFile.ps1')
        $pack += @('--signTemplate', $template, '--signParallel', '1',
            '--signExclude', '(?i)^(?!.*(?:^|[\\/])(?:Squirrel\.exe|WinhandDesktop_ExecutionStub\.exe)$).*')
    }
    Invoke-Native 'vpk pack' { vpk @pack }
    if ($signed) {
        $setup = Join-Path $releases 'winhand-win-Setup.exe'
        $signatures += Test-WindowsSignature $setup $expected
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $full = Join-Path $releases "winhand-$version-full.nupkg"
        $zipFile = [IO.Compression.ZipFile]::OpenRead($full)
        try {
            foreach ($helper in @('Squirrel.exe', 'WinhandDesktop_ExecutionStub.exe', 'WinhandDesktop.exe', 'backend/winhand.exe')) {
                $entry = $zipFile.GetEntry("lib/app/$helper")
                if ($null -eq $entry) { throw "missing from the update package: $helper" }
                $evidence = Join-Path $Output ("verify-" + [IO.Path]::GetFileName($helper))
                [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $evidence, $true)
                $signatures += Test-WindowsSignature $evidence $expected
                Remove-Item -LiteralPath $evidence
            }
        }
        finally { $zipFile.Dispose() }
    }
    Write-Host "== installer: $releases"
}
if ($signed) {
    $signatures | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $Output 'signatures.json') -Encoding utf8
    Write-Host "== $($signatures.Count) signatures verified (digest, SHA-256, RFC 3161 timestamp): $(Join-Path $Output 'signatures.json')"
    $expected.Dispose()
}
