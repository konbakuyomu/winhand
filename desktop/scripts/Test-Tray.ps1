<#
.SYNOPSIS
  Exercise the running app's notification-area icon the way a person does: right-click opens
  the menu (items are read back from the real popup), Esc closes it, "打开 winhand" and a left
  click bring the window up. Writes a screenshot of the menu next to -Out.
#>
[CmdletBinding()]
param([string] $Out = (Join-Path $PSScriptRoot '..\out\tray-menu.png'))
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
Add-Type @'
using System; using System.Text; using System.Runtime.InteropServices; using System.Collections.Generic;
public static class Tray {
  public delegate bool EnumProc(IntPtr h, IntPtr p);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc f, IntPtr p);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern uint RegisterWindowMessage(string s);
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr FindWindow(string c, string t);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern int GetMenuItemCount(IntPtr m);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetMenuString(IntPtr m, uint id, StringBuilder s, int n, uint flags);
  [DllImport("user32.dll")] public static extern uint GetMenuState(IntPtr m, uint id, uint flags);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  public struct RECT { public int L, T, R, B; }
  public static IntPtr AppWindow(uint pid) {
    IntPtr found = IntPtr.Zero;
    EnumWindows((h, p) => {
      uint owner; GetWindowThreadProcessId(h, out owner);
      var cls = new StringBuilder(256); GetClassName(h, cls, 256);
      if (owner == pid && cls.ToString() == "WinUIDesktopWin32WindowClass") { found = h; return false; }
      return true; }, IntPtr.Zero);
    return found;
  }
}
'@
[Tray]::SetProcessDPIAware() | Out-Null
$process = Get-Process WinhandDesktop -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $process) { throw 'winhand is not running' }
$hwnd = [Tray]::AppWindow([uint32]$process.Id)
if ($hwnd -eq [IntPtr]::Zero) { throw 'app window not found' }
$message = [Tray]::RegisterWindowMessage('Winhand.TrayCallback')
$iconId = [IntPtr](1 -shl 16)  # NOTIFYICON_VERSION_4: HIWORD(lParam) = icon id, LOWORD = event

function Get-Menu {
    for ($i = 0; $i -lt 30; $i++) {
        $menu = [Tray]::FindWindow('#32768', $null)
        if ($menu -ne [IntPtr]::Zero -and [Tray]::IsWindowVisible($menu)) { return $menu }
        Start-Sleep -Milliseconds 100
    }
    return [IntPtr]::Zero
}
function Send-TrayEvent([int] $event) { [Tray]::PostMessage($hwnd, $message, [IntPtr]::Zero, [IntPtr]($iconId.ToInt64() -bor $event)) | Out-Null }

$result = [ordered]@{}
# 1. right click -> menu with the expected items
Send-TrayEvent 0x0205
$menuWindow = Get-Menu
if ($menuWindow -eq [IntPtr]::Zero) { throw 'right click did not open the tray menu' }
$hmenu = [Tray]::SendMessage($menuWindow, 0x01E1, [IntPtr]::Zero, [IntPtr]::Zero)  # MN_GETHMENU
$items = @()
for ($i = 0; $i -lt [Tray]::GetMenuItemCount($hmenu); $i++) {
    $text = [Text.StringBuilder]::new(256)
    [Tray]::GetMenuString($hmenu, [uint32]$i, $text, 256, 0x400) | Out-Null  # MF_BYPOSITION
    $state = [Tray]::GetMenuState($hmenu, [uint32]$i, 0x400)
    if ($state -band 0x800) { $items += '---' }
    else { $items += ($text.ToString() + $(if ($state -band 0x3) { ' (disabled)' } else { '' })) }
}
$result.menu = $items
$rect = New-Object Tray+RECT
[Tray]::GetWindowRect($menuWindow, [ref]$rect) | Out-Null
$bitmap = New-Object Drawing.Bitmap ($rect.R - $rect.L), ($rect.B - $rect.T)
[Drawing.Graphics]::FromImage($bitmap).CopyFromScreen($rect.L, $rect.T, 0, 0, $bitmap.Size)
New-Item -ItemType Directory -Force (Split-Path $Out) | Out-Null
$bitmap.Save($Out)
$result.screenshot = $Out
foreach ($expected in @('打开 winhand', '重新连接', '断开连接', '退出 winhand')) {
    if (-not ($items | Where-Object { $_ -like "$expected*" })) { throw "menu item missing: $expected" }
}

# 2. Esc closes it
[Tray]::PostMessage($menuWindow, 0x0100, [IntPtr]0x1B, [IntPtr]::Zero) | Out-Null  # WM_KEYDOWN VK_ESCAPE
Start-Sleep -Milliseconds 500
$result.esc_closes_menu = -not [Tray]::IsWindowVisible($menuWindow)
if (-not $result.esc_closes_menu) { throw 'Esc did not close the menu' }

# 3. choosing "打开 winhand" (first item) shows the window
$wasVisible = [Tray]::IsWindowVisible($hwnd)
Send-TrayEvent 0x0205
$menuWindow = Get-Menu
[Tray]::PostMessage($menuWindow, 0x0100, [IntPtr]0x28, [IntPtr]::Zero) | Out-Null  # VK_DOWN -> first item
[Tray]::PostMessage($menuWindow, 0x0100, [IntPtr]0x0D, [IntPtr]::Zero) | Out-Null  # VK_RETURN
Start-Sleep -Milliseconds 800
$result.open_item_shows_window = [Tray]::IsWindowVisible($hwnd)
if (-not $result.open_item_shows_window) { throw '"打开 winhand" did not show the window' }

# 4. left click shows it too (checked after hiding it the way the close button does)
[Tray]::PostMessage($hwnd, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero) | Out-Null  # WM_CLOSE -> hide to tray
Start-Sleep -Milliseconds 600
$result.close_hides_to_tray = -not [Tray]::IsWindowVisible($hwnd) -and -not $process.HasExited
Send-TrayEvent 0x0202
Start-Sleep -Milliseconds 800
$result.left_click_shows_window = [Tray]::IsWindowVisible($hwnd)
if (-not $wasVisible) { [Tray]::PostMessage($hwnd, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero) | Out-Null }  # restore hidden state
$result | ConvertTo-Json
if (-not ($result.close_hides_to_tray -and $result.left_click_shows_window)) { throw 'tray window behaviour check failed' }
