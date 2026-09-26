[CmdletBinding()]
param([string] $CertificatePath = (Join-Path $PSScriptRoot '../packaging/windows/winhand.cer'))
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Windows-Signing.ps1')

if ([string]::IsNullOrWhiteSpace($env:WINHAND_WINDOWS_PFX_BASE64) -or
    [string]::IsNullOrWhiteSpace($env:WINHAND_WINDOWS_PFX_PASSWORD)) {
    throw 'Windows signing requires both WINHAND_WINDOWS_PFX_BASE64 and WINHAND_WINDOWS_PFX_PASSWORD.'
}
$expected = Get-ExpectedSigningCertificate $CertificatePath
$certificate = $null
$store = [Security.Cryptography.X509Certificates.X509Store]::new('My', 'CurrentUser')
try {
    try {
        $bytes = [Convert]::FromBase64String($env:WINHAND_WINDOWS_PFX_BASE64.Trim())
        # Validate before persisting the key. Never include the PFX/password in an exception.
        $certificate = [Security.Cryptography.X509Certificates.X509Certificate2]::new(
            $bytes, $env:WINHAND_WINDOWS_PFX_PASSWORD,
            [Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet)
    }
    catch { throw 'Unable to open the Windows signing PFX; check its encoding and password.' }
    Assert-SigningCertificate $certificate $expected
    if (-not $certificate.HasPrivateKey) { throw 'Windows signing PFX has no private key.' }
    $certificate.Dispose()
    $certificate = [Security.Cryptography.X509Certificates.X509Certificate2]::new(
        $bytes, $env:WINHAND_WINDOWS_PFX_PASSWORD,
        ([Security.Cryptography.X509Certificates.X509KeyStorageFlags]::PersistKeySet -bor
         [Security.Cryptography.X509Certificates.X509KeyStorageFlags]::UserKeySet))
    $store.Open([Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
    $store.Add($certificate)
    $installed = @($store.Certificates | Where-Object { $_.Thumbprint -eq $expected.Thumbprint -and $_.HasPrivateKey })
    if ($installed.Count -ne 1) { throw 'Signing identity was not imported as one usable current-user certificate.' }
    Write-Output "Imported Windows signing identity: $($expected.Thumbprint) (CurrentUser/My; no trust store changed)"
}
finally {
    $store.Close()
    if ($null -ne $certificate) { $certificate.Dispose() }
    $expected.Dispose()
    $bytes = $null
    # Keep child build processes from inheriting PFX secrets.
    $env:WINHAND_WINDOWS_PFX_BASE64 = $null
    $env:WINHAND_WINDOWS_PFX_PASSWORD = $null
}
