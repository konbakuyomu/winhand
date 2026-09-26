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
