using Microsoft.Win32;

namespace Winhand.Desktop;

/// <summary>Per-user app preferences (HKCU\Software\winhand\Desktop).</summary>
internal static class Preferences
{
    private const string Key = @"Software\winhand\Desktop";

    public static bool AutoCheckUpdates
    {
        get => Read("AutoCheckUpdates", 1) != 0;
        set => Write("AutoCheckUpdates", value ? 1 : 0);
    }

    public static string? ReadString(string name)
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(Key);
            return key?.GetValue(name) as string;
        }
        catch (Exception)
        {
            return null;
        }
    }

    public static void WriteString(string name, string value)
    {
        try
        {
            using var key = Registry.CurrentUser.CreateSubKey(Key, writable: true);
            key.SetValue(name, value, RegistryValueKind.String);
        }
        catch (Exception)
        {
            // a remembered pane width is a convenience
        }
    }

    private static int Read(string name, int fallback)
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(Key);
            return key?.GetValue(name) is int value ? value : fallback;
        }
        catch (Exception)
        {
            return fallback;
        }
    }

    private static void Write(string name, int value)
    {
        using var key = Registry.CurrentUser.CreateSubKey(Key, writable: true);
        key.SetValue(name, value, RegistryValueKind.DWord);
    }
}
