using Microsoft.Win32;

namespace Winhand.Desktop;

/// <summary>Start with Windows through the per-user Run key: no admin rights, runs in the
/// signed-in session with the user's full environment (PATH, toolchains, serial ports).</summary>
internal static class Autostart
{
    internal const string BackgroundArgument = "--background";
    private const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
    private const string ValueName = "winhand";

    private static string Command => $"\"{Environment.ProcessPath}\" {BackgroundArgument}";

    public static bool IsEnabled
    {
        get
        {
            using var key = Registry.CurrentUser.OpenSubKey(RunKey);
            return key?.GetValue(ValueName) is string value && value.Length > 0;
        }
    }

    /// <summary>True when the entry exists but points at another copy of the app (moved or reinstalled).</summary>
    public static bool IsStale
    {
        get
        {
            using var key = Registry.CurrentUser.OpenSubKey(RunKey);
            return key?.GetValue(ValueName) is string value && !string.Equals(value, Command, StringComparison.OrdinalIgnoreCase);
        }
    }

    public static void Set(bool enabled)
    {
        using var key = Registry.CurrentUser.CreateSubKey(RunKey, writable: true);
        if (enabled)
            key.SetValue(ValueName, Command);
        else
            key.DeleteValue(ValueName, throwOnMissingValue: false);
    }
}
