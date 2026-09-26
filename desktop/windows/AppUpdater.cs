using Velopack;
using Velopack.Sources;

namespace Winhand.Desktop;

/// <summary>Updates from the signed GitHub releases (Velopack full/delta packages).
/// Updates download in the background but are never applied behind the user's back:
/// restarting would cut Claude off mid-task. They install on "update now" or on exit.</summary>
internal sealed class AppUpdater
{
    public const string RepositoryUrl = "https://github.com/konbakuyomu/winhand";
    private readonly UpdateManager _manager = new(new GithubSource(RepositoryUrl, null, false));
    private UpdateInfo? _pending;

    public bool IsInstalled => _manager.IsInstalled;
    public string CurrentVersion => _manager.CurrentVersion?.ToString() ?? "开发版";
    public string? AvailableVersion => _pending?.TargetFullRelease.Version.ToString();
    public bool ReadyToInstall { get; private set; }
    public bool Busy { get; private set; }
    public DateTime? LastChecked { get; private set; }
    public string? LastError { get; private set; }
    public int Progress { get; private set; }

    public event EventHandler? Changed;

    /// <summary>Check, and download what is found. Returns false when nothing new.</summary>
    public async Task<bool> CheckAndDownloadAsync()
    {
        if (!IsInstalled || Busy)
            return false;
        Busy = true;
        LastError = null;
        Raise();
        try
        {
            var info = await _manager.CheckForUpdatesAsync();
            LastChecked = DateTime.Now;
            if (info is null)
                return false;
            _pending = info;
            ReadyToInstall = false;
            Raise();
            await _manager.DownloadUpdatesAsync(info, progress =>
            {
                Progress = progress;
                Raise();
            });
            ReadyToInstall = true;
            return true;
        }
        catch (Exception error)
        {
            LastError = error.Message;
            return false;
        }
        finally
        {
            Busy = false;
            Raise();
        }
    }

    /// <summary>Exit now, install, and start the new version (hidden again if we were hidden).</summary>
    public void ApplyAndRestart(bool background)
    {
        if (_pending is { } info && ReadyToInstall)
            _manager.ApplyUpdatesAndRestart(info.TargetFullRelease, background ? [Autostart.BackgroundArgument] : null);
    }

    /// <summary>Called on exit: install the downloaded version after this process ends, silently.</summary>
    public void ApplyOnExit()
    {
        if (_pending is { } info && ReadyToInstall)
            _manager.WaitExitThenApplyUpdates(info.TargetFullRelease, silent: true, restart: false);
    }

    private void Raise() => Changed?.Invoke(this, EventArgs.Empty);
}
