[CmdletBinding()]
param([Parameter(Mandatory)][string] $Path, [string] $UninstallerDirectory, [switch] $VelopackHelper)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Windows-Signing.ps1')
$certificate = Get-ExpectedSigningCertificate (Join-Path $PSScriptRoot '../packaging/windows/winhand.cer')
try {
    if ($VelopackHelper) {
        $name = [IO.Path]::GetFileName($Path)
        if ($name -notin @('Update.exe', 'Squirrel.exe', 'WinhandDesktop_ExecutionStub.exe') -and
            $name -notmatch '^winhand-win(-(x64|arm64))?-Setup\.exe$') {
            throw 'Only the known Velopack helpers and the winhand setup are allowed.'
        }
        $existing = Get-AuthenticodeSignature -LiteralPath $Path
        if ($existing.SignerCertificate -and $existing.SignerCertificate.Thumbprint -ne $certificate.Thumbprint) {
            throw 'Refusing to overwrite a third-party helper signature; inspect the new framework version first.'
        }
    }
    Invoke-WindowsSigning $Path $certificate | ConvertTo-Json -Compress | Write-Output
    if ($UninstallerDirectory -and [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($Path)) -eq [IO.Path]::GetFullPath($UninstallerDirectory)) {
        # Inno deletes its signed uninst.*.tmp after embedding it in Setup.
        $evidence = Join-Path $UninstallerDirectory 'uninstaller.exe'
        if (Test-Path -LiteralPath $evidence) { throw 'Refusing to overwrite signed uninstaller evidence.' }
        Copy-Item -LiteralPath $Path -Destination $evidence
    }
}
finally { $certificate.Dispose() }
