using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace Winhand.Desktop;

public sealed partial class MainWindow
{
    private static readonly TimeSpan UpdateInterval = TimeSpan.FromHours(6);
    private TextBlock? _updateText;
    private Button? _checkUpdateButton, _installUpdateButton;
    private ProgressBar? _updateProgress;
    private bool _updateNoticeShown;

    private void StartUpdateChecks()
    {
        _updater.Changed += (_, _) => DispatcherQueue.TryEnqueue(RenderUpdate);
        if (!_updater.IsInstalled)
            return; // running from a build folder: nothing to update
        var timer = DispatcherQueue.CreateTimer();
        timer.Interval = UpdateInterval;
        timer.Tick += async (_, _) => await CheckForUpdatesAsync(manual: false);
        timer.Start();
        // first check shortly after start, once the relay connection had its chance
        var first = DispatcherQueue.CreateTimer();
        first.Interval = TimeSpan.FromSeconds(30);
        first.IsRepeating = false;
        first.Tick += async (_, _) => await CheckForUpdatesAsync(manual: false);
        first.Start();
    }

    private async Task CheckForUpdatesAsync(bool manual)
    {
        var found = await _updater.CheckAndDownloadAsync();
        if (manual && !found && _updater.LastError is null && !_updater.ReadyToInstall)
            ShowNotice("已是最新版本", $"当前版本 {_updater.CurrentVersion}。", InfoBarSeverity.Success);
        else if (manual && _updater.LastError is { } error)
            ShowNotice("检查更新失败", error, InfoBarSeverity.Warning);
    }

    private async Task UpdateNowAsync()
    {
        if (!_updater.ReadyToInstall || _shuttingDown)
            return;
        _shuttingDown = true;
        _ticker.Stop();
        await _backend.StopAsync(); // end sessions and the tunnel cleanly before files are replaced
        _tray.Dispose();
        _allowClose = true;
        _updater.ApplyAndRestart(background: !IsWindowVisible(_hwnd));
    }

    private UIElement BuildUpdateCard()
    {
        _updateText = Text("", "SecondaryCopyStyle");
        _updateProgress = new ProgressBar { Minimum = 0, Maximum = 100, Visibility = Visibility.Collapsed };
        _checkUpdateButton = ActionButton("检查更新", () => _ = CheckForUpdatesAsync(manual: true));
        _installUpdateButton = ActionButton("立即更新并重启", () => _ = UpdateNowAsync(), accent: true);
        var buttons = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8, Children = { _checkUpdateButton, _installUpdateButton } };
        RenderUpdate();
        return Card(new StackPanel
        {
            Spacing = 8,
            Children = { Text("更新", "SectionHeadingStyle"), _updateText, _updateProgress, buttons }
        });
    }

    private void RenderUpdate()
    {
        if (_updateText is null)
            return;
        string status;
        if (!_updater.IsInstalled)
            status = "当前是从构建目录运行的开发版，不支持自动更新。用安装器安装后可自动更新。";
        else if (_updater.Busy && _updater.AvailableVersion is { } downloading)
            status = $"正在下载 {downloading}… {_updater.Progress}%";
        else if (_updater.Busy)
            status = "正在检查更新…";
        else if (_updater.ReadyToInstall)
            status = $"新版本 {_updater.AvailableVersion} 已下载。点“立即更新并重启”，或下次从托盘退出时自动安装。";
        else if (_updater.LastError is { } error)
            status = $"上次检查失败：{error}";
        else
            status = $"当前版本 {_updater.CurrentVersion}，已是最新" +
                (_updater.LastChecked is { } at ? $"（{at:HH:mm} 检查）" : "") + "。每 6 小时自动检查一次。";
        _updateText.Text = status;
        _updateProgress!.Visibility = _updater.Busy && _updater.AvailableVersion is not null ? Visibility.Visible : Visibility.Collapsed;
        _updateProgress.Value = _updater.Progress;
        _checkUpdateButton!.IsEnabled = _updater.IsInstalled && !_updater.Busy;
        _installUpdateButton!.Visibility = _updater.ReadyToInstall ? Visibility.Visible : Visibility.Collapsed;

        if (_updater.ReadyToInstall && !_updateNoticeShown)
        {
            _updateNoticeShown = true;
            ShowNotice($"新版本 {_updater.AvailableVersion} 已下载",
                "现在更新会重启 winhand（Claude 的连接会断开几秒）；也可以等下次退出时自动安装。", InfoBarSeverity.Informational);
            var install = new Button { Content = "立即更新" };
            install.Click += (_, _) => _ = UpdateNowAsync();
            Notice.ActionButton = install;
        }
        RefreshStatus();
    }

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern bool IsWindowVisible(nint hWnd);
}
