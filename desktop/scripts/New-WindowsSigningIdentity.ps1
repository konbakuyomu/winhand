[CmdletBinding()]
param(
    [string] $PrivateDirectory = (Join-Path $env:LOCALAPPDATA 'winhand/signing/windows'),
    [string] $CertificatePath = (Join-Path $PSScriptRoot '../packaging/windows/winhand.cer')
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $IsWindows) { throw 'Run this command in PowerShell 7 on Windows.' }
$privateRoot = [IO.Path]::GetFullPath($PrivateDirectory)
$repositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
if ($privateRoot.StartsWith($repositoryRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or $privateRoot -eq $repositoryRoot) {
    throw 'Private signing material must stay outside the repository.'
}
$pfxPath = Join-Path $privateRoot 'winhand.pfx'
$passwordPath = Join-Path $privateRoot 'password.dpapi'
$publicPath = [IO.Path]::GetFullPath($CertificatePath)
foreach ($path in @($privateRoot, $publicPath, [IO.Path]::ChangeExtension($publicPath, '.json'))) {
    if (Test-Path -LiteralPath $path) { throw "Refusing to replace an existing signing identity: $path" }
}
$ancestor = Split-Path $privateRoot -Parent
while ($ancestor) {
    if (Test-Path -LiteralPath $ancestor) {
        if ((Get-Item -LiteralPath $ancestor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Private signing directory must not traverse a link or junction.'
        }
    }
    $ancestor = Split-Path $ancestor -Parent
}
New-Item -ItemType Directory -Path $privateRoot -Force | Out-Null
$acl = [Security.AccessControl.DirectorySecurity]::new()
$acl.SetAccessRuleProtection($true, $false)
$owner = [Security.Principal.WindowsIdentity]::GetCurrent().User
$acl.SetOwner($owner)
$rule = [Security.AccessControl.FileSystemAccessRule]::new($owner, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
$acl.AddAccessRule($rule)
Set-Acl -LiteralPath $privateRoot -AclObject $acl

$rsa = [Security.Cryptography.RSA]::Create(3072)
$certificate = $null
try {
    $request = [Security.Cryptography.X509Certificates.CertificateRequest]::new(
        'CN=winhand', $rsa, [Security.Cryptography.HashAlgorithmName]::SHA256, [Security.Cryptography.RSASignaturePadding]::Pkcs1)
    $request.CertificateExtensions.Add([Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]::new($false, $false, 0, $true))
    $request.CertificateExtensions.Add([Security.Cryptography.X509Certificates.X509KeyUsageExtension]::new('DigitalSignature', $true))
    $usages = [Security.Cryptography.OidCollection]::new()
    [void]$usages.Add([Security.Cryptography.Oid]::new('1.3.6.1.5.5.7.3.3'))
    $request.CertificateExtensions.Add([Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension]::new($usages, $true))
    $certificate = $request.CreateSelfSigned([DateTimeOffset]::UtcNow.AddMinutes(-5), [DateTimeOffset]::UtcNow.AddYears(3))
    $password = [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
    $securePassword = ConvertTo-SecureString $password -AsPlainText -Force
    # DPAPI binds the backup password to this Windows user; never print it.
    $securePassword | ConvertFrom-SecureString | Set-Content -LiteralPath $passwordPath -Encoding utf8
    [IO.File]::WriteAllBytes($pfxPath, $certificate.Export([Security.Cryptography.X509Certificates.X509ContentType]::Pfx, $password))
    New-Item -ItemType Directory -Path (Split-Path $publicPath -Parent) -Force | Out-Null
    [IO.File]::WriteAllBytes($publicPath, $certificate.Export([Security.Cryptography.X509Certificates.X509ContentType]::Cert))
    [ordered]@{
        subject = $certificate.Subject
        sha256 = $certificate.GetCertHashString([Security.Cryptography.HashAlgorithmName]::SHA256)
        not_after = $certificate.NotAfter.ToUniversalTime().ToString('o')
        trust = 'self-signed; not trusted by Windows by default'
    } | ConvertTo-Json | Set-Content -LiteralPath ([IO.Path]::ChangeExtension($publicPath, '.json')) -Encoding utf8
    Write-Output "Public certificate: $publicPath"
    Write-Output "Encrypted private backup: $privateRoot (password protected by current-user DPAPI)"
}
finally {
    if ($null -ne $certificate) { $certificate.Dispose() }
    $rsa.Dispose()
    $password = $null
}
