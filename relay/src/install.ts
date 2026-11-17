// The script behind `irm https://<relay>/i/<code> | iex`: installs winhand on a Windows PC and pairs
// it with this relay as one named computer. Works in Windows PowerShell 5.1 and PowerShell 7.
// It runs inside the user's own PowerShell (iex), so it never calls `exit`: everything is inside
// one script block that returns on failure.

const REPO = "konbakuyomu/winhand";
// Inno Setup registers winhand under this key ("Installed apps"); see desktop/packaging/windows/winhand.iss.
const UNINSTALL_KEY = "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{BD773DCA-E6D2-427C-92E1-5900C6FF13F1}_is1";

const quote = (value: string) => `'${value.replace(/'/g, "''")}'`;

export function installScript(origin: string, code: string): string {
  return `# winhand: install and pair this computer with ${origin}
& {
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$relay = ${quote(origin)}
$code = ${quote(code)}
function Say($text) { Write-Host "winhand: $text" }
function Fail($text) { Write-Host "winhand: $text" -ForegroundColor Red }

# 1. Use the one-time code up: this computer's name, device token and addresses.
try {
  $pair = Invoke-RestMethod -Method Post -Uri "$relay/i/$code" -UseBasicParsing
} catch {
  Fail "这条安装命令已经用过或过期了。请到 $relay 重新生成。"
  return
}
Say "这台电脑在中转上的名字：$($pair.name)"

# 2. The newest release: its installer and SHA-256, from the release's SHA256SUMS.txt.
$setup = $env:WINHAND_INSTALLER
$version = $null
if (-not $setup) {
  $sums = (Invoke-WebRequest -Uri 'https://github.com/${REPO}/releases/latest/download/SHA256SUMS.txt' -UseBasicParsing).Content
  if ($sums -is [byte[]]) { $sums = [Text.Encoding]::UTF8.GetString($sums) }
  $pattern = '^([0-9a-fA-F]{64})\\s+\\*?(winhand-([0-9.]+)-Setup\\.exe)\\s*$'
  $line = $sums -split "\`n" | Where-Object { $_ -match $pattern } | Select-Object -First 1
  if (-not $line -or $line -notmatch $pattern) { Fail '在最新版本里没找到安装包。'; return }
  $hash = $Matches[1]; $file = $Matches[2]; $version = $Matches[3]
}

# 3. Install unless this version is already there.
$installed = Get-ItemProperty -Path ${quote(UNINSTALL_KEY)} -ErrorAction SilentlyContinue
if ($version -and $installed -and $installed.DisplayVersion -eq $version -and (Test-Path (Join-Path $installed.InstallLocation 'current\\backend\\winhand.exe'))) {
  Say "已经装着 winhand $version，跳过安装。"
  Get-Process WinhandDesktop -ErrorAction SilentlyContinue | Stop-Process -Force
} else {
  if (-not $setup) {
    $setup = Join-Path $env:TEMP $file
    Say "下载 $file ……"
    Invoke-WebRequest -Uri "https://github.com/${REPO}/releases/download/v$version/$file" -OutFile $setup -UseBasicParsing
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $setup).Hash -ne $hash.ToUpper()) { Fail '安装包的 SHA-256 对不上，没有安装。'; return }
  }
  Say '安装中……'
  $run = Start-Process -FilePath $setup -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/MERGETASKS=autostart' -Wait -PassThru
  if ($run.ExitCode -ne 0) { Fail "安装程序退出码 $($run.ExitCode)。"; return }
  if (-not $env:WINHAND_INSTALLER) { Remove-Item -LiteralPath $setup -ErrorAction SilentlyContinue }
  $installed = Get-ItemProperty -Path ${quote(UNINSTALL_KEY)} -ErrorAction SilentlyContinue
}
$dir = if ($installed -and $installed.InstallLocation) { $installed.InstallLocation.TrimEnd('\\') } else { Join-Path $env:LOCALAPPDATA 'Programs\\winhand' }
$backend = Join-Path $dir 'current\\backend\\winhand.exe'
if (-not (Test-Path $backend)) { Fail "没找到 $backend。"; return }

# 4. Save the relay address and device token (the token goes in on stdin, never on a command line;
#    UTF-8 without a BOM, which Windows PowerShell would otherwise put in front of it).
$OutputEncoding = New-Object System.Text.UTF8Encoding $false
$pair.token | & $backend login --url $pair.agent_url --token-stdin
if ($LASTEXITCODE -ne 0) { Fail '保存地址和令牌失败：这个版本的 winhand 太旧，装上新版后再运行一次这条命令。'; return }

# 5. Start with Windows (the same entry the app and the installer write) and start now, through
#    Explorer like the installer does, so winhand does not inherit this shell's process settings.
$app = Join-Path $dir 'current\\WinhandDesktop.exe'
Set-ItemProperty -Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run' -Name 'winhand' -Value ('"' + $app + '" --background')
if (-not $env:WINHAND_NO_START) { Start-Process -FilePath (Join-Path $env:WINDIR 'explorer.exe') -ArgumentList ('"' + (Join-Path $dir 'winhand.exe') + '"') }

# 6. Wait until the relay sees it.
$online = $false
if (-not $env:WINHAND_NO_START) {
  for ($i = 0; $i -lt 30 -and -not $online; $i++) {
    Start-Sleep -Seconds 2
    try { $online = [bool](Invoke-RestMethod -Uri $pair.status_url -UseBasicParsing).online } catch {}
  }
}
if ($online) { Say "电脑 $($pair.name) 已经连上中转。" } else { Say "装好了，但还没看到它连上：看托盘里 winhand 的状态，或打开 $relay 。" }
Say '在 claude.ai 的「设置 → 连接器 → 添加自定义连接器」里填这个地址，授权时输入主人口令：'
Write-Host "  $($pair.mcp_url)" -ForegroundColor Green
}
`;
}

export function expiredScript(origin: string): string {
  return `Write-Host ${quote(`winhand: 这条安装命令已经用过或过期了。请到 ${origin} 重新生成。`)} -ForegroundColor Red\n`;
}
