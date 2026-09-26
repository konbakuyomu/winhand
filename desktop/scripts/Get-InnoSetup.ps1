<#
.SYNOPSIS
  Return the path of ISCC.exe (Inno Setup 6.7.3), fetching the official, pinned release when needed.

  The download is checked against a pinned SHA-256 and the publisher's Authenticode signature
  (Pyrsys B.V.) before it runs, then installed in portable mode under desktop/out/tools/inno:
  nothing is registered on the machine.
#>
[CmdletBinding()]
param([string] $ToolsDirectory = (Join-Path $PSScriptRoot '..\out\tools'))
$ErrorActionPreference = 'Stop'
$version = '6.7.3'
$url = "https://github.com/jrsoftware/issrc/releases/download/is-6_7_3/innosetup-$version.exe"
$sha256 = '9C73C3BAE7ED48D44112A0F48E66742C00090BDB5BEF71D9D3C056C66E97B732'

$ToolsDirectory = [IO.Path]::GetFullPath($ToolsDirectory)
$target = Join-Path $ToolsDirectory 'inno'
$iscc = Join-Path $target 'ISCC.exe'
if (Test-Path -LiteralPath $iscc) { return $iscc }

New-Item -ItemType Directory -Force -Path $ToolsDirectory | Out-Null
$installer = Join-Path $ToolsDirectory "innosetup-$version.exe"
if (-not (Test-Path -LiteralPath $installer)) {
    Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing
}
$actual = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash
if ($actual -ne $sha256) { Remove-Item -LiteralPath $installer; throw "Inno Setup download SHA-256 mismatch: $actual" }
$signature = Get-AuthenticodeSignature -LiteralPath $installer
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notlike '*Pyrsys B.V.*') {
    throw 'Inno Setup download is not validly signed by Pyrsys B.V.'
}
$process = Start-Process -FilePath $installer -Wait -PassThru -ArgumentList @(
    '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CURRENTUSER', '/PORTABLE=1', '/NOICONS', "/DIR=`"$target`"")
if ($process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $iscc)) { throw "Inno Setup install failed (exit $($process.ExitCode))" }
return $iscc
