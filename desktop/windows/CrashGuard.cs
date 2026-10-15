using System.Runtime.InteropServices;
using System.Text;

namespace Winhand.Desktop;

/// <summary>The app owns the backend: when it exits, the relay connection goes with it and Claude
/// loses this computer until someone opens winhand again. So an error is written to
/// logs\desktop-app.log instead of ending the app where that is possible (App marks the ones XAML
/// reports as handled), and Windows starts the app again, in the notification area, after a
/// crash nothing could catch.</summary>
internal static class CrashGuard
{
    private const int MaxEntries = 20; // per run: an error repeating every second must not fill the disk
    private const long MaxLogBytes = 1_000_000;
    private const int RestartNoPatch = 4, RestartNoReboot = 8;
    private static readonly object Gate = new();
    private static int _entries;

    public static string LogPath { get; } = Path.Combine(
        Environment.GetEnvironmentVariable("WINHAND_HOME") is { Length: > 0 } home
            ? home
            : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".winhand"),
        "logs", "desktop-app.log");

    public static void Install()
    {
        AppDomain.CurrentDomain.UnhandledException += (_, e) => Write("fatal", e.ExceptionObject as Exception, null);
        TaskScheduler.UnobservedTaskException += (_, e) =>
        {
            Write("task", e.Exception, null);
            e.SetObserved();
        };
        try
        {
            // Windows does this only for a process that ran at least a minute: a start that fails
            // at once is not retried over and over
            RegisterApplicationRestart(Autostart.BackgroundArgument, RestartNoPatch | RestartNoReboot);
        }
        catch (Exception)
        {
            // restarting after a crash is a safety net, not a requirement
        }
    }

    public static void Write(string kind, Exception? error, string? message)
    {
        try
        {
            lock (Gate)
            {
                if (++_entries > MaxEntries)
                    return;
                Directory.CreateDirectory(Path.GetDirectoryName(LogPath)!);
                if (File.Exists(LogPath) && new FileInfo(LogPath).Length > MaxLogBytes)
                    File.Move(LogPath, LogPath + ".1", overwrite: true);
                var entry = new StringBuilder()
                    .Append($"{DateTime.Now:yyyy-MM-dd HH:mm:ss.fff} {kind} v{typeof(CrashGuard).Assembly.GetName().Version}")
                    .Append($" 0x{(error?.HResult ?? 0):X8} {error?.GetType().FullName}\n");
                if (!string.IsNullOrEmpty(message) && message != error?.Message)
                    entry.Append(message).Append('\n');
                entry.Append(error?.ToString() ?? "(no exception object)").Append("\n\n");
                File.AppendAllText(LogPath, entry.ToString(), new UTF8Encoding(false));
            }
        }
        catch (Exception)
        {
            // writing the log must never become the next crash
        }
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern int RegisterApplicationRestart(string commandLine, int flags);
}
