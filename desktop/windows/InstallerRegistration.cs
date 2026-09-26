using Microsoft.Win32;

namespace Winhand.Desktop;

/// <summary>The setup wizard (Inno Setup) registers winhand under "Installed apps", but updates
/// then happen inside the app (Velopack). Keep the version shown there in step with what runs.</summary>
internal static class InstallerRegistration
{
    private const string UninstallKey =
        @"Software\Microsoft\Windows\CurrentVersion\Uninstall\{BD773DCA-E6D2-427C-92E1-5900C6FF13F1}_is1";

    public static void Sync()
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(UninstallKey, writable: true);
            if (key?.GetValue("InstallLocation") is not string location)
                return;
            // this process runs from <install>\current\
            var install = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, ".."));
            if (!string.Equals(Path.TrimEndingDirectorySeparator(Path.GetFullPath(location)),
                    Path.TrimEndingDirectorySeparator(install), StringComparison.OrdinalIgnoreCase))
                return;
            var version = System.Reflection.Assembly.GetExecutingAssembly().GetName().Version?.ToString(3);
            if (version is not null && key.GetValue("DisplayVersion") as string != version)
            {
                key.SetValue("DisplayVersion", version);
                key.SetValue("DisplayName", "winhand");
            }
        }
        catch (Exception)
        {
            // cosmetic only; never block startup
        }
    }
}
