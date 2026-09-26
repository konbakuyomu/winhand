using System.Runtime.InteropServices;

namespace Winhand.Desktop;

/// <summary>The notification-area icon. It is always present while the app runs: the tray is
/// where winhand lives, the window is only a view onto it.</summary>
internal sealed class NativeTray : IDisposable
{
    public sealed record MenuItem(string Text, Action Invoke, bool Enabled = true);

    private const uint NimAdd = 0x0, NimModify = 0x1, NimDelete = 0x2, NimSetVersion = 0x4;
    private const uint NifMessage = 0x1, NifIcon = 0x2, NifTip = 0x4, NifShowTip = 0x80;
    private const int GwlWndProc = -4;
    private const int WmLButtonUp = 0x0202, WmLButtonDblClk = 0x0203, WmRButtonUp = 0x0205, WmContextMenu = 0x007B;
    private const uint MfString = 0x0, MfGrayed = 0x1, MfSeparator = 0x800;
    private const uint TpmReturnCmd = 0x100, TpmRightButton = 0x2, TpmBottomAlign = 0x20;
    private const uint NotifyIconVersion4 = 4;

    internal const string CallbackMessageName = "Winhand.TrayCallback";
    private readonly nint _windowHandle;
    private readonly uint _callbackMessage;
    private readonly uint _taskbarCreated;
    private readonly WndProc _windowProcedure;
    private readonly nint _previousWindowProcedure;
    private readonly Action _open;
    private readonly Func<IReadOnlyList<MenuItem?>> _menu;
    private readonly nint _onlineIcon, _offlineIcon;
    private NotifyIconData _icon;
    private bool _visible;

    public NativeTray(nint windowHandle, Action open, Func<IReadOnlyList<MenuItem?>> menu)
    {
        _windowHandle = windowHandle;
        _open = open;
        _menu = menu;
        // A fixed name so desktop/scripts/Test-Tray.ps1 can simulate clicks; one app instance at a time.
        _callbackMessage = RegisterWindowMessage(CallbackMessageName);
        _taskbarCreated = RegisterWindowMessage("TaskbarCreated");
        _windowProcedure = OnMessage;
        _previousWindowProcedure = SetWindowLongPtr(_windowHandle, GwlWndProc, Marshal.GetFunctionPointerForDelegate(_windowProcedure));
        _onlineIcon = LoadTrayIcon("winhand.ico");
        _offlineIcon = LoadTrayIcon("winhand-offline.ico");
        _icon = new NotifyIconData
        {
            cbSize = Marshal.SizeOf<NotifyIconData>(),
            hWnd = _windowHandle,
            uID = 1,
            uFlags = NifMessage | NifIcon | NifTip | NifShowTip,
            uCallbackMessage = _callbackMessage,
            hIcon = _offlineIcon,
            szTip = "winhand",
            uTimeoutOrVersion = NotifyIconVersion4
        };
    }

    public void Show()
    {
        if (_visible)
            return;
        _visible = ShellNotifyIcon(NimAdd, ref _icon);
        if (_visible)
            ShellNotifyIcon(NimSetVersion, ref _icon);
    }

    public void Hide()
    {
        if (!_visible)
            return;
        ShellNotifyIcon(NimDelete, ref _icon);
        _visible = false;
    }

    /// <summary>Colour icon while connected, grey otherwise; the tooltip carries the detail.</summary>
    public void Update(bool online, string tooltip)
    {
        _icon.hIcon = online ? _onlineIcon : _offlineIcon;
        _icon.szTip = tooltip.Length > 127 ? tooltip[..127] : tooltip;
        if (_visible)
            ShellNotifyIcon(NimModify, ref _icon);
    }

    private nint OnMessage(nint hWnd, uint message, nint wParam, nint lParam)
    {
        if (message == _taskbarCreated && _visible)
        {
            // Explorer restarted and forgot every icon; add ours back.
            _visible = false;
            Show();
        }
        else if (message == _callbackMessage)
        {
            // NOTIFYICON_VERSION_4: the event is in the low word of lParam.
            var trayEvent = (int)(lParam.ToInt64() & 0xFFFF);
            if (trayEvent is WmLButtonUp or WmLButtonDblClk)
                _open();
            else if (trayEvent is WmRButtonUp or WmContextMenu)
                ShowMenu();
            return nint.Zero;
        }
        return CallWindowProc(_previousWindowProcedure, hWnd, message, wParam, lParam);
    }

    private void ShowMenu()
    {
        var items = _menu();
        var menu = CreatePopupMenu();
        try
        {
            for (var i = 0; i < items.Count; i++)
            {
                if (items[i] is { } item)
                    AppendMenu(menu, MfString | (item.Enabled ? 0 : MfGrayed), (nuint)(i + 1), item.Text);
                else
                    AppendMenu(menu, MfSeparator, 0, null);
            }
            GetCursorPos(out var point);
            SetForegroundWindow(_windowHandle); // otherwise the menu does not close when clicking elsewhere
            var chosen = TrackPopupMenuEx(menu, TpmReturnCmd | TpmRightButton | TpmBottomAlign, point.X, point.Y, _windowHandle, nint.Zero);
            if (chosen > 0 && items[chosen - 1] is { Enabled: true } picked)
                picked.Invoke();
        }
        finally
        {
            DestroyMenu(menu);
        }
    }

    private static nint LoadTrayIcon(string file)
    {
        var size = GetSystemMetrics(49); // SM_CXSMICON, already scaled for the primary monitor's DPI
        var icon = LoadImage(nint.Zero, Path.Combine(AppContext.BaseDirectory, "Assets", file), 1, size, size, 0x10);
        return icon != nint.Zero ? icon : LoadIcon(nint.Zero, new nint(32512));
    }

    public void Dispose()
    {
        if (_visible)
        {
            ShellNotifyIcon(NimDelete, ref _icon);
            _visible = false;
        }
        DestroyIcon(_onlineIcon);
        DestroyIcon(_offlineIcon);
        if (_previousWindowProcedure != nint.Zero)
            SetWindowLongPtr(_windowHandle, GwlWndProc, _previousWindowProcedure);
    }

    private delegate nint WndProc(nint hWnd, uint message, nint wParam, nint lParam);

    [StructLayout(LayoutKind.Sequential)]
    private struct Point { public int X; public int Y; }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct NotifyIconData
    {
        public int cbSize;
        public nint hWnd;
        public uint uID;
        public uint uFlags;
        public uint uCallbackMessage;
        public nint hIcon;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string szTip;
        public uint dwState;
        public uint dwStateMask;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string szInfo;
        public uint uTimeoutOrVersion;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 64)] public string szInfoTitle;
        public uint dwInfoFlags;
        public Guid guidItem;
        public nint hBalloonIcon;
    }

    [DllImport("shell32.dll", EntryPoint = "Shell_NotifyIconW", CharSet = CharSet.Unicode)]
    private static extern bool ShellNotifyIcon(uint message, ref NotifyIconData data);

    [DllImport("user32.dll", EntryPoint = "RegisterWindowMessageW", CharSet = CharSet.Unicode)]
    private static extern uint RegisterWindowMessage(string value);

    [DllImport("user32.dll", EntryPoint = "LoadIconW", CharSet = CharSet.Unicode)]
    private static extern nint LoadIcon(nint instance, nint iconName);

    [DllImport("user32.dll", EntryPoint = "LoadImageW", CharSet = CharSet.Unicode)]
    private static extern nint LoadImage(nint instance, string name, uint type, int width, int height, uint flags);

    [DllImport("user32.dll")]
    private static extern int GetSystemMetrics(int index);

    [DllImport("user32.dll")]
    private static extern bool DestroyIcon(nint icon);

    [DllImport("user32.dll")]
    private static extern nint CreatePopupMenu();

    [DllImport("user32.dll", EntryPoint = "AppendMenuW", CharSet = CharSet.Unicode)]
    private static extern bool AppendMenu(nint menu, uint flags, nuint id, string? text);

    [DllImport("user32.dll")]
    private static extern int TrackPopupMenuEx(nint menu, uint flags, int x, int y, nint hWnd, nint parameters);

    [DllImport("user32.dll")]
    private static extern bool DestroyMenu(nint menu);

    [DllImport("user32.dll")]
    private static extern bool GetCursorPos(out Point point);

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(nint hWnd);

    [DllImport("user32.dll", EntryPoint = "SetWindowLongPtrW", SetLastError = true)]
    private static extern nint SetWindowLongPtr(nint hWnd, int index, nint newValue);

    [DllImport("user32.dll", EntryPoint = "CallWindowProcW", SetLastError = true)]
    private static extern nint CallWindowProc(nint previous, nint hWnd, uint message, nint wParam, nint lParam);
}
