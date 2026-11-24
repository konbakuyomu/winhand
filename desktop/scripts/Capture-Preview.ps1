param(
    [string]$OutDir = (Join-Path $PSScriptRoot '..\out\preview\shots'),
    [string[]]$Pages = @('overview', 'activity', 'sessions', 'mcp', 'settings'),
    [ValidateSet('default', 'empty')][string]$Scenario = 'default',
    [string]$Size = '853x1018',
    [switch]$NoBuild
)
$ErrorActionPreference = 'Stop'
$Pages = $Pages -split ','  # pwsh -File passes "a,b" as one string
$desktop = Split-Path $PSScriptRoot -Parent
$app = Join-Path $desktop 'out\preview\app'
$backend = Join-Path $PSScriptRoot 'preview_backend.py'
if (-not $NoBuild) {
    dotnet publish (Join-Path $desktop 'windows\Winhand.Desktop.csproj') -c Release -r win-x64 --self-contained true -p:Platform=x64 -o $app
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
[IO.Directory]::CreateDirectory([IO.Path]::GetFullPath($OutDir)) | Out-Null
Add-Type -AssemblyName System.Drawing
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class PreviewCapture {
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll", SetLastError=true)] public static extern bool PrintWindow(IntPtr hwnd, IntPtr hdc, uint flags);
    [DllImport("user32.dll", SetLastError=true)] public static extern bool GetWindowRect(IntPtr hwnd, out RECT rect);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll", SetLastError=true)] public static extern IntPtr SetThreadDpiAwarenessContext(IntPtr context);
}
'@
$previousDpi = [PreviewCapture]::SetThreadDpiAwarenessContext([IntPtr]::new(-4))
if ($previousDpi -eq 0) { throw [ComponentModel.Win32Exception]::new([Runtime.InteropServices.Marshal]::GetLastWin32Error()) }
try {
    foreach ($page in $Pages) {
        $env:WINHAND_DESKTOP_INSTANCE = 'preview'
        $env:WINHAND_BACKEND_PATH = (Get-Command python).Source
        $env:WINHAND_BACKEND_ARGS = $backend
        $env:WINHAND_PREVIEW_SCENARIO = $Scenario
        $env:WINHAND_DESKTOP_PAGE = $page
        $env:WINHAND_DESKTOP_SIZE = $Size
        $foreground = [PreviewCapture]::GetForegroundWindow()
        $changed = $false
        $process = Start-Process (Join-Path $app 'WinhandDesktop.exe') -PassThru -WindowStyle Hidden
        try {
            $deadline = [DateTime]::UtcNow.AddSeconds(30)
            do {
                $process.Refresh()
                $changed = $changed -or ([PreviewCapture]::GetForegroundWindow() -ne $foreground)
                if ($process.HasExited) { throw "Preview process exited: $($process.ExitCode)" }
                if ($process.MainWindowHandle -ne 0) { break }
                if ([DateTime]::UtcNow -ge $deadline) { throw 'Preview window did not appear within 30 seconds' }
                Start-Sleep -Milliseconds 100
            } while ($true)
            $ready = [DateTime]::UtcNow.AddSeconds(3)
            do {
                Start-Sleep -Milliseconds 100
                $changed = $changed -or ([PreviewCapture]::GetForegroundWindow() -ne $foreground)
            } while ([DateTime]::UtcNow -lt $ready)
            $rect = [PreviewCapture+RECT]::new()
            if (-not [PreviewCapture]::GetWindowRect($process.MainWindowHandle, [ref]$rect)) {
                throw [ComponentModel.Win32Exception]::new([Runtime.InteropServices.Marshal]::GetLastWin32Error())
            }
            $width = $rect.Right - $rect.Left
            $height = $rect.Bottom - $rect.Top
            $bitmap = [Drawing.Bitmap]::new($width, $height)
            try {
                $graphics = [Drawing.Graphics]::FromImage($bitmap)
                $hdc = $graphics.GetHdc()
                try {
                    if (-not [PreviewCapture]::PrintWindow($process.MainWindowHandle, $hdc, 2)) {
                        throw [ComponentModel.Win32Exception]::new([Runtime.InteropServices.Marshal]::GetLastWin32Error())
                    }
                } finally { $graphics.ReleaseHdc($hdc); $graphics.Dispose() }
                $bitmap.Save((Join-Path ([IO.Path]::GetFullPath($OutDir)) "$Scenario-$page.png"), [Drawing.Imaging.ImageFormat]::Png)
                $color = $bitmap.GetPixel(32, 80).ToArgb()
                $content = $false
                for ($y = 80; $y -lt $height - 32 -and -not $content; $y += 32) {
                    for ($x = 32; $x -lt $width - 32; $x += 32) {
                        if ($bitmap.GetPixel($x, $y).ToArgb() -ne $color) { $content = $true; break }
                    }
                }
                if (-not $content) { throw "Blank or black preview: $Scenario-$page.png" }
            } finally { $bitmap.Dispose() }
            $changed = $changed -or ([PreviewCapture]::GetForegroundWindow() -ne $foreground)
        } finally {
            $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($process.Id) AND Name='python.exe'" |
                Where-Object { $_.CommandLine -like "*$backend*" } | ForEach-Object { [Diagnostics.Process]::GetProcessById($_.ProcessId) })
            if (-not $process.HasExited) { Stop-Process -Id $process.Id; $process.WaitForExit() }
            foreach ($child in $children) {
                try {
                    if (-not $child.WaitForExit(5000)) { throw "Preview backend did not exit: $($child.Id)" }
                } finally { $child.Dispose() }
            }
            $process.Dispose()
        }
        $changed = $changed -or ([PreviewCapture]::GetForegroundWindow() -ne $foreground)
        $answer = if ($changed) { '是' } else { '否' }
        Write-Output "$page ${width}x${height} 前台窗口变过：$answer"
    }
} finally { [PreviewCapture]::SetThreadDpiAwarenessContext($previousDpi) | Out-Null }
