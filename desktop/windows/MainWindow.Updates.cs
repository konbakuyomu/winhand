using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace Winhand.Desktop;

public sealed partial class MainWindow
{
    private static readonly TimeSpan UpdateInterval = TimeSpan.FromHours(6);
    private TextBlock? _updateSummary, _updateFacts, _updateDetail, _updateNotes;
    private Border? _updateNotesBox;
    private Button? _updateAction, _updateCancel;
    private ToggleSwitch? _autoCheckSwitch;
    private ProgressBar? _updateProgress;
    private CancellationTokenSource? _downloadCancellation;
    private string? _announcedVersion;
    private bool _installingUpdate;

    private void StartUpdateChecks()
    {
        _updater.Changed += (_, _) => DispatcherQueue.TryEnqueue(RenderUpdate);
        if (!_updater.Installed)
            return; // running from a build folder
        var first = DispatcherQueue.CreateTimer();
        first.Interval = TimeSpan.FromSeconds(30); // after the relay connection had its chance
        first.IsRepeating = false;
        first.Tick += async (_, _) => await CheckAutomaticallyAsync();
        first.Start();
        var timer = DispatcherQueue.CreateTimer();
        timer.Interval = UpdateInterval;
        timer.Tick += async (_, _) => await CheckAutomaticallyAsync();
        timer.Start();
    }

    private async Task CheckAutomaticallyAsync()
    {
        if (_shuttingDown || !Preferences.AutoCheckUpdates || _updater.Ready || _updater.Downloading)
            return;
        await _updater.CheckAsync();
        AnnounceUpdate();
    }

    private void AnnounceUpdate()
    {
        if (!_updater.Available || _updater.LatestVersion == _announcedVersion)
            return;
        _announcedVersion = _updater.LatestVersion;
        ShowNotice($"发现新版本 {_updater.LatestVersion}", "可以在设置里查看更新内容，确认后再下载。", InfoBarSeverity.Informational);
        Notice.ActionButton = NoticeButton("查看", () => ShowPage("settings"));
    }

    /// <summary>The card's main button walks through the steps, like Smart Search.</summary>
    private async Task UpdateActionAsync()
    {
        if (!_updater.Installed)
            OpenPath(AppUpdater.RepositoryUrl + "/releases/latest");
        else if (_updater.Ready)
            await InstallUpdateAsync();
        else if (_updater.Available)
            await DownloadUpdateAsync();
        else
        {
            await _updater.CheckAsync();
            if (_updater.Error.Length > 0)
                ShowNotice("检查更新失败", _updater.Error, InfoBarSeverity.Warning);
            else if (!_updater.Available)
                ShowNotice("已是最新版本", $"当前版本 {_updater.CurrentVersion}。", InfoBarSeverity.Success);
            else
                _announcedVersion = _updater.LatestVersion;
        }
    }

    private async Task DownloadUpdateAsync()
    {
        using var cancellation = new CancellationTokenSource();
        _downloadCancellation = cancellation;
        try
        {
            await _updater.DownloadAsync(cancellation.Token);
        }
        finally
        {
            _downloadCancellation = null;
        }
        if (_updater.Ready)
        {
            ShowNotice($"新版本 {_updater.LatestVersion} 已就绪",
                "现在重启会断开 Claude 的连接几秒；也可以等下次退出时自动安装。", InfoBarSeverity.Success);
            Notice.ActionButton = NoticeButton("重启并完成更新", () => _ = InstallUpdateAsync());
        }
        else if (_updater.Error.Length > 0)
            ShowNotice("更新未下载", _updater.Error, InfoBarSeverity.Warning);
    }

    private async Task InstallUpdateAsync()
    {
        if (!_updater.Ready || _shuttingDown || _installingUpdate)
            return;
        _installingUpdate = true;
        RenderUpdate();
        var background = !IsWindowVisible(_hwnd);
        _shuttingDown = true;
        _ticker.Stop();
        try
        {
            await _backend.StopAsync(); // end sessions and the tunnel before files are replaced
            _tray.Hide();
            _allowClose = true;
            _updater.ApplyAndRestart(background);
        }
        catch (Exception error)
        {
            // stay on the current version and bring everything back
            _allowClose = false;
            _shuttingDown = false;
            _installingUpdate = false;
            _tray.Show();
            _ticker.Start();
            _restartAttempts = 0;
            await StartBackendAsync();
            ShowNotice("更新未完成", $"已恢复连接，当前版本保持不变。{error.Message}", InfoBarSeverity.Error);
            RenderUpdate();
        }
    }

    private UIElement BuildUpdateCard()
    {
        _updateSummary = Text("", "SectionHeadingStyle");
        _updateFacts = Text("", "SecondaryCopyStyle");
        _updateDetail = Text("", "SecondaryCopyStyle");
        _updateProgress = new ProgressBar { Minimum = 0, Maximum = 100, Visibility = Visibility.Collapsed };
        _updateNotes = Text("", "BodyCopyStyle");
        _updateNotes.IsTextSelectionEnabled = true;
        _updateNotesBox = new Border
        {
            Child = new ScrollViewer { Content = _updateNotes, MaxHeight = 220 },
            Padding = new Thickness(12),
            CornerRadius = new CornerRadius(6),
            Background = Resource<Microsoft.UI.Xaml.Media.Brush>("ControlFillColorSecondaryBrush"),
            Visibility = Visibility.Collapsed
        };
        _updateAction = ActionButton("检查更新", () => _ = UpdateActionAsync(), accent: true);
        _updateCancel = ActionButton("取消下载", () => _downloadCancellation?.Cancel());
        var history = new HyperlinkButton { Content = "所有版本与更新记录", NavigateUri = new Uri(AppUpdater.RepositoryUrl + "/releases") };
        var buttons = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8, Children = { _updateAction, _updateCancel, history } };

        _autoCheckSwitch = new ToggleSwitch { IsOn = Preferences.AutoCheckUpdates, OnContent = "开", OffContent = "关" };
        _autoCheckSwitch.Toggled += (_, _) =>
        {
            Preferences.AutoCheckUpdates = _autoCheckSwitch.IsOn;
            if (_autoCheckSwitch.IsOn)
                _ = CheckAutomaticallyAsync();
        };
        var autoRow = new Grid { ColumnSpacing = 16 };
        autoRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        autoRow.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        autoRow.Children.Add(new StackPanel
        {
            Children = { Text("自动检查更新"), Text("启动后和每 6 小时检查一次。发现新版本只会提醒你，确认后才下载。", "SecondaryCopyStyle") }
        });
        Grid.SetColumn(_autoCheckSwitch, 1);
        autoRow.Children.Add(_autoCheckSwitch);

        RenderUpdate();
        return Card(new StackPanel
        {
            Spacing = 10,
            Children =
            {
                Text("更新", "SecondaryCopyStyle"), _updateSummary, _updateFacts, _updateNotesBox, _updateProgress,
                buttons, _updateDetail, new Border { Style = Resource<Style>("SectionDividerStyle") }, autoRow
            }
        });
    }

    private void RenderUpdate()
    {
        if (_updateSummary is null)
            return;
        _updateSummary.Text = !_updater.Installed ? "这个副本不能自动更新"
            : _installingUpdate ? "正在安装更新…"
            : _updater.Checking ? "正在检查更新…"
            : _updater.Downloading ? $"正在下载 {_updater.LatestVersion}：{_updater.Progress}%"
            : _updater.Ready ? $"新版本 {_updater.LatestVersion} 已就绪"
            : _updater.Available ? $"发现新版本 {_updater.LatestVersion}"
            : _updater.Error.Length > 0 ? "检查更新失败"
            : _updater.CheckedAt is null ? "尚未检查更新"
            : "已是最新版本";

        var facts = $"当前版本 {_updater.CurrentVersion}";
        if (_updater.LatestVersion is { } latest)
            facts += $" · 最新版本 {latest}";
        if (_updater.CheckedAt is { } at)
            facts += $" · 上次检查 {at:MM-dd HH:mm}";
        _updateFacts!.Text = facts;

        var notes = _updater.ReleaseNotes;
        _updateNotes!.Text = notes.Length > 0 ? $"{_updater.LatestVersion} 更新内容\n\n{notes}" : "";
        _updateNotesBox!.Visibility = _updater.Available && notes.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
        _updateProgress!.Visibility = _updater.Downloading ? Visibility.Visible : Visibility.Collapsed;
        _updateProgress.Value = _updater.Progress;

        _updateAction!.Content = !_updater.Installed ? "下载安装包"
            : _updater.Ready ? "重启并完成更新"
            : _updater.Available ? "下载更新"
            : "检查更新";
        _updateAction.IsEnabled = !_installingUpdate && !_updater.Checking && !_updater.Downloading;
        _updateCancel!.Visibility = _updater.Downloading ? Visibility.Visible : Visibility.Collapsed;
        _autoCheckSwitch!.IsEnabled = _updater.Installed;
        _updateDetail!.Text = !_updater.Installed
            ? "当前是从构建目录运行的副本。用安装包安装正式版后，就能在这里检查和安装更新；配置与记录会保留。"
            : _updater.Error.Length > 0 ? _updater.Error
            : _updater.Ready ? "重启只需几秒，Claude 的连接会自动恢复。不想现在重启，下次从托盘退出时会自动安装。"
            : "更新包来自 GitHub Releases，带 winhand 签名；配置、令牌和活动记录不受影响。";
        RefreshStatus();
    }

    private static Button NoticeButton(string text, Action onClick)
    {
        var button = new Button { Content = text };
        button.Click += (_, _) => onClick();
        return button;
    }

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern bool IsWindowVisible(nint hWnd);
}
