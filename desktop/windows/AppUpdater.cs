using System.Runtime.InteropServices;
using Velopack;
using Velopack.Sources;

namespace Winhand.Desktop;

/// <summary>App updates from the signed GitHub releases, through Velopack (the same framework as
/// Smart Search). Check, download and install are separate steps: nothing downloads until the
/// user agrees, and installing restarts winhand, which drops Claude's connection for a moment.</summary>
internal sealed class AppUpdater
{
    public const string RepositoryUrl = "https://github.com/konbakuyomu/winhand";
    private readonly UpdateManager _manager;
    private UpdateInfo? _update;

    public AppUpdater()
    {
        var channel = RuntimeInformation.ProcessArchitecture == Architecture.Arm64 ? "win-arm64" : "win";
        _manager = new UpdateManager(new GithubSource(RepositoryUrl, null, false),
            new UpdateOptions { ExplicitChannel = channel, AllowVersionDowngrade = false });
    }

    public bool Installed => _manager.IsInstalled;
    public string CurrentVersion => _manager.CurrentVersion?.ToString()
        ?? System.Reflection.Assembly.GetExecutingAssembly().GetName().Version?.ToString(3) ?? "?";
    public string? LatestVersion => _update?.TargetFullRelease.Version.ToString();
    public string ReleaseNotes => _update?.TargetFullRelease.NotesMarkdown?.Trim() ?? "";
    public bool Checking { get; private set; }
    public bool Downloading { get; private set; }
    public bool Ready { get; private set; }
    public bool Available => _update is not null;
    public bool CanCheck => Installed && !Checking && !Downloading;
    public string Error { get; private set; } = "";
    public int Progress { get; private set; }
    public DateTime? CheckedAt { get; private set; }

    public event EventHandler? Changed;

    public async Task CheckAsync()
    {
        if (!CanCheck)
            return;
        Checking = true;
        Error = "";
        Raise();
        try
        {
            var found = await _manager.CheckForUpdatesAsync();
            // keep a finished download if the same version is still the newest
            if (found is null || found.TargetFullRelease.Version.ToString() != LatestVersion)
                Ready = false;
            _update = found;
            CheckedAt = DateTime.Now;
        }
        catch (Exception error)
        {
            Error = $"无法检查更新：{error.Message}";
        }
        finally
        {
            Checking = false;
            Raise();
        }
    }

    public async Task DownloadAsync(CancellationToken cancellationToken)
    {
        if (_update is not { } update || Downloading || Ready)
            return;
        Downloading = true;
        Progress = 0;
        Error = "";
        Raise();
        try
        {
            await _manager.DownloadUpdatesAsync(update, value => { Progress = value; Raise(); }, cancellationToken);
            cancellationToken.ThrowIfCancellationRequested();
            Ready = true;
        }
        catch (OperationCanceledException)
        {
            Error = "下载已取消。";
        }
        catch (Exception error)
        {
            Error = $"下载或校验失败，当前版本保持不变：{error.Message}";
        }
        finally
        {
            Downloading = false;
            Raise();
        }
    }

    /// <summary>Exit, install, and start the new version (hidden again if it was running hidden).</summary>
    public void ApplyAndRestart(bool background)
    {
        if (!Ready || _update is null)
            throw new InvalidOperationException("没有已下载的更新");
        _manager.ApplyUpdatesAndRestart(_update.TargetFullRelease, background ? [Autostart.BackgroundArgument] : null);
    }

    /// <summary>On exit: install a finished download once this process is gone, silently.</summary>
    public void ApplyOnExit()
    {
        if (Ready && _update is not null)
            _manager.WaitExitThenApplyUpdates(_update.TargetFullRelease, silent: true, restart: false);
    }

    private void Raise() => Changed?.Invoke(this, EventArgs.Empty);
}
