using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace Winhand.Desktop;

public sealed partial class MainWindow
{
    private static readonly TimeSpan UpdateInterval = TimeSpan.FromHours(6);
    private TextBlock? _updateSummary, _updateFacts, _updateDetail;
    private StackPanel? _updateNotes;
    private string? _renderedNotes;
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
        ShowNotice($"发现新版本 {_updater.LatestVersion}", "可以在设置里查看更新内容，确认后再下载。", InfoBarSeverity.Informational,
            "查看", () => ShowPage("settings"));
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
                "现在重启会断开 Claude 的连接几秒；也可以等下次退出时自动安装。", InfoBarSeverity.Success,
                "重启并完成更新", () => _ = InstallUpdateAsync());
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

    /// <summary>The release notes as headings, bullets and paragraphs (they arrive as Markdown).</summary>
    private void RenderNotes(string? version, string notes)
    {
        var key = $"{version}\n{notes}";
        if (_updateNotes is null || key == _renderedNotes)
            return; // RenderUpdate runs on every progress tick; rebuild only when the notes change
        _renderedNotes = key;
        _updateNotes.Children.Clear();
        if (notes.Length == 0)
            return;
        _updateNotes.Children.Add(Text($"{version} 更新内容", "LabelCopyStyle"));
        foreach (var line in ReleaseNotes.Parse(notes, version))
        {
            var text = Text(line.Text, line.Kind == NoteKind.Heading ? "LabelCopyStyle" : "BodyCopyStyle");
            text.IsTextSelectionEnabled = true;
            if (line.Kind == NoteKind.Heading)
            {
                text.Margin = new Thickness(0, 6, 0, 0);
                _updateNotes.Children.Add(text);
            }
            else if (line.Kind == NoteKind.Bullet)
            {
                var row = new Grid { ColumnSpacing = 8 };
                row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
                row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                row.Children.Add(Text("•", "BodyCopyStyle"));
                Grid.SetColumn(text, 1);
                row.Children.Add(text);
                _updateNotes.Children.Add(row);
            }
            else
                _updateNotes.Children.Add(text);
        }
    }

    private UIElement BuildUpdateCard()
    {
        _updateSummary = Text("", "LabelCopyStyle");
        _updateFacts = Text("", "SecondaryCopyStyle");
        _updateDetail = Text("", "SecondaryCopyStyle");
        _updateProgress = new ProgressBar { Minimum = 0, Maximum = 100, Visibility = Visibility.Collapsed };
        _updateNotes = new StackPanel { Spacing = 6 };
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
        history.Padding = new Thickness(8, 0, 8, 0);
        var buttons = ActionRow(_updateAction, _updateCancel, history);

        _autoCheckSwitch = CompactSwitch("自动检查更新", Preferences.AutoCheckUpdates);
        _autoCheckSwitch.Toggled += (_, _) =>
        {
            Preferences.AutoCheckUpdates = _autoCheckSwitch.IsOn;
            if (_autoCheckSwitch.IsOn)
                _ = CheckAutomaticallyAsync();
        };
        var autoRow = SettingRow("自动检查更新", "启动后和每 6 小时检查一次。发现新版本只会提醒你，确认后才下载。", _autoCheckSwitch);

        RenderUpdate();
        var summary = new StackPanel { Spacing = 4, Children = { _updateSummary, _updateFacts } };
        return Card(new StackPanel
        {
            Spacing = 12,
            Children = { summary, _updateNotesBox, _updateProgress, buttons, _updateDetail, Divider(), autoRow }
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
        RenderNotes(_updater.LatestVersion, notes);
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

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern bool IsWindowVisible(nint hWnd);
}
