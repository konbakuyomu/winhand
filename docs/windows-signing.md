# Windows 签名与信任

winhand 的 Windows 程序使用**自签名代码签名证书**。签名覆盖：App（`WinhandDesktop.exe/.dll`）、后端（`backend\winhand.exe`）、Velopack 的 `Update.exe`、执行桩和 `winhand-win-Setup.exe`。签名算法为 SHA-256，并带 RFC 3161 时间戳。第三方文件（Windows App SDK、.NET、Python）保留原始字节，不重新签名。

- 证书：[`desktop/packaging/windows/winhand.cer`](../desktop/packaging/windows/winhand.cer)（`CN=winhand`，有效期至 2029-09-26）
- SHA-256：`711736B2D92DBF2B9ECE1A22B7003A9D49CC5BC918B39987FFBF56A9AB1DBEC2`

## 在自己的电脑上信任（可选）

Windows 默认不信任自签名证书。若只在自己电脑上使用，先核对上面的指纹，再运行：

```powershell
./desktop/scripts/Trust-WindowsSigningCertificate.ps1          # 信任（Windows 会弹窗确认）
./desktop/scripts/Trust-WindowsSigningCertificate.ps1 -Remove  # 撤销
```

脚本只导入公开证书，写入当前用户的“受信任的根证书颁发机构”和“受信任的发布者”。之后 `Get-AuthenticodeSignature` 显示 `Valid`，属性页和 UAC 显示发布者 `winhand`。

- **SmartScreen**：信任证书不会产生 SmartScreen 信誉。用浏览器下载的 Setup.exe 仍可能提示“已保护你的电脑”，确认来源后选“更多信息 → 仍要运行”即可，也可以对下载的文件运行 `Unblock-File`。App 内自动更新不经过 SmartScreen。
- **代价**：信任后，任何拿到这把私钥的人签的程序在这台电脑上也会被视为可信。私钥只保存在维护者电脑的受保护目录和 GitHub Secrets 里。

## 维护者

- 生成身份（只做一次，已完成）：`New-WindowsSigningIdentity.ps1`。PFX 在 `%LOCALAPPDATA%\winhand\signing\windows`，只有当前用户能访问；密码用当前 Windows 用户的 DPAPI 加密保存。
- GitHub Secrets：`WINHAND_WINDOWS_PFX_BASE64`、`WINHAND_WINDOWS_PFX_PASSWORD`。
- `desktop.yml`：推送到 main 时，先运行 `Test-WindowsSigning.ps1`（拒绝错误密码/证书、过期、内容/签名/时间戳被篡改、缺少时间戳），再签名构建。拉取请求不签名。
- `release.yml`：推送 `vX.Y.Z` tag 时签名构建，逐个验签，然后发布到 GitHub Releases，附带 `SHA256SUMS.txt` 和 `signatures.json`。已安装的 App 通过这些 Release 自动更新。
- 本地签名构建：`./desktop/scripts/Build-Windows.ps1 -Installer -SigningMode Required`（需要 pwsh 7，并先把身份导入 `CurrentUser\My`）。
