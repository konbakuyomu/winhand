Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-ExpectedSigningCertificate([string] $Path) {
    $certificate = [Security.Cryptography.X509Certificates.X509Certificate2]::new([IO.File]::ReadAllBytes([IO.Path]::GetFullPath($Path)))
    if ($certificate.HasPrivateKey) { throw 'The repository certificate must not contain a private key.' }
    return $certificate
}

function Assert-SigningCertificate($Certificate, $Expected) {
    if ($Certificate.GetCertHashString([Security.Cryptography.HashAlgorithmName]::SHA256) -ne
        $Expected.GetCertHashString([Security.Cryptography.HashAlgorithmName]::SHA256)) {
        throw 'Windows signing certificate does not match the repository public certificate.'
    }
    if ($Certificate.NotBefore -gt (Get-Date) -or $Certificate.NotAfter -le (Get-Date)) { throw 'Windows signing certificate is outside its validity period.' }
    $eku = @($Certificate.Extensions | Where-Object { $_ -is [Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension] })
    if ($eku.Count -ne 1 -or '1.3.6.1.5.5.7.3.3' -notin @($eku[0].EnhancedKeyUsages | ForEach-Object Value)) {
        throw 'Windows signing certificate does not explicitly permit code signing.'
    }
    $chain = [Security.Cryptography.X509Certificates.X509Chain]::new()
    try {
        $chain.ChainPolicy.TrustMode = [Security.Cryptography.X509Certificates.X509ChainTrustMode]::CustomRootTrust
        [void]$chain.ChainPolicy.CustomTrustStore.Add($Expected)
        [void]$chain.ChainPolicy.ApplicationPolicy.Add([Security.Cryptography.Oid]::new('1.3.6.1.5.5.7.3.3'))
        $chain.ChainPolicy.RevocationMode = [Security.Cryptography.X509Certificates.X509RevocationMode]::NoCheck
        if (-not $chain.Build($Certificate)) { throw 'The pinned Windows signing certificate chain is invalid.' }
    }
    finally { $chain.Dispose() }
}

function Get-WindowsSignTool {
    $command = Get-Command signtool.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Source }
    $kitRoot = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits/10/bin'
    $architecture = if ([Runtime.InteropServices.RuntimeInformation]::OSArchitecture -eq 'Arm64') { 'arm64' } else { 'x64' }
    $paths = @(Get-ChildItem -LiteralPath $kitRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^\d+(\.\d+){3}$' } |
        Sort-Object { [version]$_.Name } -Descending |
        ForEach-Object { Join-Path $_.FullName "$architecture/signtool.exe" } |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
    if ($paths.Count -eq 0) { throw 'Windows SDK signtool.exe is required for signed builds.' }
    return $paths[0]
}

function Get-EmbeddedSignature([string] $Path) {
    $stream = [IO.File]::OpenRead($Path)
    $reader = [IO.BinaryReader]::new($stream)
    try {
        if ($stream.Length -lt 128 -or $reader.ReadUInt16() -ne 0x5A4D) { throw 'Expected a Windows PE file.' }
        $stream.Position = 60
        $peOffset = $reader.ReadUInt32()
        if ($peOffset -gt $stream.Length - 152) { throw 'Invalid PE header offset.' }
        $stream.Position = $peOffset
        if ($reader.ReadUInt32() -ne 0x4550) { throw 'Invalid PE signature.' }
        $stream.Position = $peOffset + 24
        $magic = $reader.ReadUInt16()
        $directoryOffset = switch ($magic) { 0x10B { 96 }; 0x20B { 112 }; default { throw 'Unsupported PE header.' } }
        $stream.Position = $peOffset + 24 + $directoryOffset + 32
        $offset = $reader.ReadUInt32()
        $size = $reader.ReadUInt32()
        if ($size -lt 8 -or $size -gt 1MB -or $offset -lt 128 -or [long]$offset + $size -gt $stream.Length) {
            throw 'Missing or invalid embedded Authenticode certificate table.'
        }
        $stream.Position = $offset
        $length = $reader.ReadUInt32()
        if ($length -lt 8 -or $length -gt $size -or $reader.ReadUInt16() -ne 0x200 -or $reader.ReadUInt16() -ne 2) {
            throw 'Invalid Authenticode PKCS#7 entry.'
        }
        $cms = [Security.Cryptography.Pkcs.SignedCms]::new()
        $cms.Decode($reader.ReadBytes($length - 8))
        if ($cms.ContentInfo.ContentType.Value -ne '1.3.6.1.4.1.311.2.1.4' -or $cms.SignerInfos.Count -ne 1) {
            throw 'Expected one embedded Authenticode signer.'
        }
        return $cms
    }
    finally { $reader.Dispose(); $stream.Dispose() }
}

function Test-WindowsSignature([string] $Path, $Expected) {
    $Path = [IO.Path]::GetFullPath($Path)
    if (-not ('WindowsAuthenticode' -as [type])) { Add-Type -Path (Join-Path $PSScriptRoot 'WindowsAuthenticode.cs') }
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($null -eq $signature.SignerCertificate) { throw "Missing Authenticode signer: $Path" }
    Assert-SigningCertificate $signature.SignerCertificate $Expected
    $cms = Get-EmbeddedSignature $Path
    $signer = $cms.SignerInfos[0]
    Assert-SigningCertificate $signer.Certificate $Expected
    if ($signer.DigestAlgorithm.Value -ne '2.16.840.1.101.3.4.2.1') { throw 'Authenticode must use SHA-256.' }
    $cms.CheckSignature($true)
    # Check the actual PE digest separately from chain policy. A valid certificate
    # chain alone does not prove that the executable or its CMS signature is intact.
    $hashResult = [WindowsAuthenticode]::Verify($Path, 0x210)
    if ($hashResult -ne 0) { throw ('Authenticode file digest failed: 0x{0:X8}' -f $hashResult) }
    $trustResult = [WindowsAuthenticode]::Verify($Path, 0x10)
    if ($trustResult -ne 0 -and $trustResult -ne 0x800B0109u) {
        throw ('Authenticode policy failed: 0x{0:X8}' -f $trustResult)
    }

    $timestamps = @($signer.UnsignedAttributes | Where-Object { $_.Oid.Value -eq '1.3.6.1.4.1.311.3.3.1' })
    if ($timestamps.Count -ne 1 -or $timestamps[0].Values.Count -ne 1) { throw 'Exactly one RFC3161 timestamp is required.' }
    $rawToken = $timestamps[0].Values[0].RawData
    [Security.Cryptography.Pkcs.Rfc3161TimestampToken]$token = $null
    [int]$consumed = 0
    if (-not [Security.Cryptography.Pkcs.Rfc3161TimestampToken]::TryDecode(
        [ReadOnlyMemory[byte]]::new($rawToken), [ref]$token, [ref]$consumed) -or $consumed -ne $rawToken.Length) {
        throw 'Invalid RFC3161 timestamp encoding.'
    }
    [Security.Cryptography.X509Certificates.X509Certificate2]$timestampCertificate = $null
    if (-not $token.VerifySignatureForSignerInfo($signer, [ref]$timestampCertificate, $null)) {
        throw 'Timestamp signature does not match the Authenticode signature.'
    }
    $timestamp = $token.TokenInfo.Timestamp.UtcDateTime
    if ($timestamp -lt $Expected.NotBefore.ToUniversalTime() -or $timestamp -gt $Expected.NotAfter.ToUniversalTime() -or
        $timestamp -gt [DateTime]::UtcNow.AddMinutes(5)) { throw 'Timestamp is outside the valid signing period.' }
    $chain = [Security.Cryptography.X509Certificates.X509Chain]::new()
    try {
        $chain.ChainPolicy.VerificationTime = $timestamp
        $chain.ChainPolicy.RevocationMode = [Security.Cryptography.X509Certificates.X509RevocationMode]::NoCheck
        [void]$chain.ChainPolicy.ApplicationPolicy.Add([Security.Cryptography.Oid]::new('1.3.6.1.5.5.7.3.8'))
        $chain.ChainPolicy.ExtraStore.AddRange($token.AsSignedCms().Certificates)
        if (-not $chain.Build($timestampCertificate)) { throw 'Timestamp certificate does not chain to a trusted authority.' }
    }
    finally { $chain.Dispose(); $timestampCertificate.Dispose() }
    return [ordered]@{
        path = $Path
        sha256 = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
        certificate_sha256 = $Expected.GetCertHashString([Security.Cryptography.HashAlgorithmName]::SHA256)
        integrity = 'verified'
        timestamp = $timestamp.ToString('o')
        windows_trust = if ($trustResult -eq 0) { 'trusted-on-this-machine' } else { 'untrusted-self-signed-root' }
    }
}

function Invoke-WindowsSigning([string] $Path, $Expected) {
    $tool = Get-WindowsSignTool
    $certificatePath = "Cert:/CurrentUser/My/$($Expected.Thumbprint)"
    if (-not (Test-Path -LiteralPath $certificatePath) -or -not (Get-Item -LiteralPath $certificatePath).HasPrivateKey) {
        throw 'Import the pinned signing identity before requesting a signed build.'
    }
    & $tool sign /sha1 $Expected.Thumbprint /s My /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 ([IO.Path]::GetFullPath($Path)) | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Windows signing failed; unsigned fallback is forbidden.' }
    return Test-WindowsSignature $Path $Expected
}
