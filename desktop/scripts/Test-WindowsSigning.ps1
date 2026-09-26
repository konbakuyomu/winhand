[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Windows-Signing.ps1')
$certificatePath = Join-Path $PSScriptRoot '../packaging/windows/winhand.cer'
$expected = Get-ExpectedSigningCertificate $certificatePath
$pfx = $env:WINHAND_WINDOWS_PFX_BASE64
$password = $env:WINHAND_WINDOWS_PFX_PASSWORD
if ([string]::IsNullOrWhiteSpace($pfx) -or [string]::IsNullOrWhiteSpace($password)) {
    throw 'The signing check needs PFX secrets to exercise password and import failures.'
}
$runDirectory = Join-Path (Join-Path $PSScriptRoot '../out') ('signing-check-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $runDirectory | Out-Null
$results = [Collections.Generic.List[string]]::new()

function Assert-Rejected([string] $Name, [scriptblock] $Action, [string] $MessagePattern) {
    try { & $Action | Out-Null }
    catch {
        if ($_.Exception.Message -notmatch $MessagePattern) { throw "Unexpected failure in $Name`: $($_.Exception.Message)" }
        $results.Add($Name)
        Write-Output "PASS: $Name"
        return
    }
    throw "Expected rejection did not occur: $Name"
}

function Write-CorruptSignature([byte[]] $Bytes, [byte[]] $Signature, [string] $Destination) {
    $offset = [Convert]::ToHexString($Bytes).IndexOf([Convert]::ToHexString($Signature), [StringComparison]::Ordinal)
    if ($offset -lt 0 -or $offset % 2 -ne 0) { throw 'Cannot find signature bytes in test PE.' }
    $Bytes[$offset / 2 + 12] = $Bytes[$offset / 2 + 12] -bxor 1
    [IO.File]::WriteAllBytes($Destination, $Bytes)
}

try {
    $rootsBefore = @(Get-ChildItem Cert:/CurrentUser/Root, Cert:/CurrentUser/TrustedPublisher | ForEach-Object Thumbprint | Sort-Object)
    $env:WINHAND_WINDOWS_PFX_BASE64 = $null
    $env:WINHAND_WINDOWS_PFX_PASSWORD = $null
    Assert-Rejected 'missing-secrets' { & (Join-Path $PSScriptRoot 'Import-WindowsSigningIdentity.ps1') } 'requires both'
    $env:WINHAND_WINDOWS_PFX_BASE64 = $pfx
    $env:WINHAND_WINDOWS_PFX_PASSWORD = 'deliberately-wrong-test-password'
    Assert-Rejected 'wrong-password' { & (Join-Path $PSScriptRoot 'Import-WindowsSigningIdentity.ps1') } 'Unable to open'
    $env:WINHAND_WINDOWS_PFX_BASE64 = [Convert]::ToBase64String($expected.Export([Security.Cryptography.X509Certificates.X509ContentType]::Pfx, 'public-only'))
    $env:WINHAND_WINDOWS_PFX_PASSWORD = 'public-only'
    Assert-Rejected 'no-private-key' { & (Join-Path $PSScriptRoot 'Import-WindowsSigningIdentity.ps1') } 'no private key'

    $rsa = [Security.Cryptography.RSA]::Create(2048)
    try {
        $request = [Security.Cryptography.X509Certificates.CertificateRequest]::new('CN=Signing rejection test', $rsa,
            [Security.Cryptography.HashAlgorithmName]::SHA256, [Security.Cryptography.RSASignaturePadding]::Pkcs1)
        $wrong = $request.CreateSelfSigned([DateTimeOffset]::UtcNow.AddMinutes(-1), [DateTimeOffset]::UtcNow.AddDays(1))
        try {
            $env:WINHAND_WINDOWS_PFX_BASE64 = [Convert]::ToBase64String($wrong.Export([Security.Cryptography.X509Certificates.X509ContentType]::Pfx, 'wrong-identity'))
            $env:WINHAND_WINDOWS_PFX_PASSWORD = 'wrong-identity'
            Assert-Rejected 'wrong-certificate' { & (Join-Path $PSScriptRoot 'Import-WindowsSigningIdentity.ps1') } 'does not match'
            Assert-Rejected 'wrong-certificate-usage' { Assert-SigningCertificate $wrong $wrong } 'permit code signing'
        }
        finally { $wrong.Dispose() }
        $expired = $request.CreateSelfSigned([DateTimeOffset]::UtcNow.AddDays(-2), [DateTimeOffset]::UtcNow.AddDays(-1))
        try { Assert-Rejected 'expired-certificate' { Assert-SigningCertificate $expired $expired } 'validity period' }
        finally { $expired.Dispose() }
    }
    finally { $rsa.Dispose() }
    $env:WINHAND_WINDOWS_PFX_BASE64 = $null
    $env:WINHAND_WINDOWS_PFX_PASSWORD = $null

    $probe = Join-Path $runDirectory 'probe.dll'
    Add-Type -TypeDefinition ('public class SigningProbe' + [guid]::NewGuid().ToString('N') + ' { public static int Value => 42; }') -OutputAssembly $probe
    $noTimestamp = Join-Path $runDirectory 'no-timestamp.dll'
    Copy-Item -LiteralPath $probe -Destination $noTimestamp
    $signed = Invoke-WindowsSigning $probe $expected
    $results.Add('valid-signature-and-rfc3161-timestamp')
    # CI machines never trust the certificate; a maintainer machine may (Trust-WindowsSigningCertificate.ps1).
    if ($env:CI -and $signed.windows_trust -ne 'untrusted-self-signed-root') { throw 'This check expects the signing certificate to be absent from system trust stores.' }
    $results.Add("trust-reported:$($signed.windows_trust)")

    $bytes = [IO.File]::ReadAllBytes($probe)
    $bytes[64] = $bytes[64] -bxor 1
    $tampered = Join-Path $runDirectory 'tampered.dll'
    [IO.File]::WriteAllBytes($tampered, $bytes)
    Assert-Rejected 'modified-pe-content' { Test-WindowsSignature $tampered $expected } 'digest failed|Missing Authenticode'

    $cms = Get-EmbeddedSignature $probe
    $badSignature = Join-Path $runDirectory 'bad-signature.dll'
    Write-CorruptSignature ([IO.File]::ReadAllBytes($probe)) $cms.SignerInfos[0].GetSignature() $badSignature
    Assert-Rejected 'modified-cms-signature' { Test-WindowsSignature $badSignature $expected } 'Invalid signature|Missing Authenticode'

    $timestampAttribute = @($cms.SignerInfos[0].UnsignedAttributes | Where-Object { $_.Oid.Value -eq '1.3.6.1.4.1.311.3.3.1' })[0]
    $timestampCms = [Security.Cryptography.Pkcs.SignedCms]::new()
    $timestampCms.Decode($timestampAttribute.Values[0].RawData)
    $badTimestamp = Join-Path $runDirectory 'bad-timestamp.dll'
    Write-CorruptSignature ([IO.File]::ReadAllBytes($probe)) $timestampCms.SignerInfos[0].GetSignature() $badTimestamp
    Assert-Rejected 'modified-timestamp-signature' { Test-WindowsSignature $badTimestamp $expected } 'Timestamp signature|policy failed|Missing Authenticode'

    & (Get-WindowsSignTool) sign /sha1 $expected.Thumbprint /s My /fd SHA256 $noTimestamp | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Unable to prepare timestamp rejection probe.' }
    Assert-Rejected 'missing-timestamp' { Test-WindowsSignature $noTimestamp $expected } 'timestamp is required'
    Assert-Rejected 'sign-tool-failure' { Invoke-WindowsSigning (Join-Path $runDirectory 'missing.dll') $expected } 'Windows signing failed'
    $rootsAfter = @(Get-ChildItem Cert:/CurrentUser/Root, Cert:/CurrentUser/TrustedPublisher | ForEach-Object Thumbprint | Sort-Object)
    if (@(Compare-Object $rootsBefore $rootsAfter).Count -ne 0) { throw 'Signing checks changed a user trust store.' }
    $results.Add('user-trust-stores-unchanged')
    [ordered]@{ checks = $results.ToArray(); signature = $signed; result = 'passed' } |
        ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $runDirectory 'result.json') -Encoding utf8
    Write-Output "Signing checks passed ($($results.Count)): $runDirectory"
}
finally {
    $expected.Dispose()
    $pfx = $password = $null
    $env:WINHAND_WINDOWS_PFX_BASE64 = $null
    $env:WINHAND_WINDOWS_PFX_PASSWORD = $null
}
# The expected failing signtool probe leaves LASTEXITCODE=1. GitHub's pwsh
# wrapper forwards that value unless this successful check exits explicitly.
exit 0
