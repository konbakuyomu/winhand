using System.Diagnostics;
using System.Runtime.InteropServices;

namespace Winhand.Desktop;

/// <summary>Some launchers (Inno Setup's "run after install", installers and updaters started from
/// them) hand their RedirectionGuard mitigation down to the processes they start. With it,
/// winhand and every shell it opens would refuse to follow junctions a normal user created,
/// which breaks scoop, mise and similar tool managers. Such a start is redone through
/// Explorer, the way a person's double click would start it.</summary>
internal static class CleanLaunch
{
    private static string Marker => Path.Combine(Path.GetTempPath(), "winhand-relaunch.json");

    /// <summary>True when this process must exit because a clean copy is being started.</summary>
    public static bool RelaunchIfRestricted(bool background)
    {
        if (!RedirectionTrustEnforced())
            return false;
        try
        {
            // guard against a loop if even Explorer's children carry the mitigation
            if (File.Exists(Marker) && DateTime.UtcNow - File.GetLastWriteTimeUtc(Marker) < TimeSpan.FromSeconds(30))
                return false;
            File.WriteAllText(Marker, background ? "background" : "window");
            Process.Start(new ProcessStartInfo(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "explorer.exe"), $"\"{Environment.ProcessPath}\"")
            {
                UseShellExecute = false
            });
            return true;
        }
        catch (Exception)
        {
            return false; // run restricted rather than not at all
        }
    }

    /// <summary>Whether the previous (restricted) instance asked this one to start hidden.</summary>
    public static bool TakeBackgroundRequest()
    {
        try
        {
            if (!File.Exists(Marker))
                return false;
            var fresh = DateTime.UtcNow - File.GetLastWriteTimeUtc(Marker) < TimeSpan.FromSeconds(30);
            var background = File.ReadAllText(Marker).Trim() == "background";
            if (!RedirectionTrustEnforced())
                File.Delete(Marker);
            return fresh && background;
        }
        catch (Exception)
        {
            return false;
        }
    }

    private static bool RedirectionTrustEnforced()
    {
        try
        {
            return GetProcessMitigationPolicy(GetCurrentProcess(), 16 /* ProcessRedirectionTrustPolicy */, out var flags, 4)
                && (flags & 1) != 0;
        }
        catch (Exception)
        {
            return false; // older Windows: no such policy
        }
    }

    [DllImport("kernel32.dll")]
    private static extern nint GetCurrentProcess();

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetProcessMitigationPolicy(nint process, int policy, out uint value, nuint size);
}
