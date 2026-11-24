using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Shapes;

namespace Winhand.Desktop;

/// <summary>The relay page and local settings (startup, updates, local data, about).</summary>
public sealed partial class MainWindow
{
    private TextBox? _relayUrl;
    private PasswordBox? _relayToken;
    private ToggleSwitch? _autostart;
    private TextBlock? _configPath, _logsPath, _activityPath, _aboutText, _autostartHint;
    private bool _relayUrlTouched;
    private Button? _relaySave;
    private TextBlock? _relayUrlError, _relayHost, _relayDuration, _relayConfig;
    private ContentControl? _relayBadge;
    private InfoBar? _relayError, _autostartWarning;
    private StackPanel? _relayTokenSaved;
    private Grid? _relayUptimeRow;

    private UIElement BuildRelay()
    {
        var page = PagePanel();
        page.Spacing = 16;
        _relayBadge = new ContentControl { Style = Resource<Style>("StatusBadgeNeutralStyle") };
        page.Children.Add(PageControlRow(Text("中转", "PageTitleStyle"), _relayBadge));
        _relayError = new InfoBar { Severity = InfoBarSeverity.Error, IsClosable = false, IsOpen = true, Visibility = Visibility.Collapsed };
        page.Children.Add(_relayError);
        _relayUrl = new TextBox { Header = "中转地址", PlaceholderText = "wss://winhand.example.com/agent", FontFamily = new FontFamily("Cascadia Mono, Consolas") };
        _relayUrlError = Text("地址要以 wss:// 或 ws:// 开头", "CaptionTextStyle");
        _relayUrlError.Foreground = Resource<Brush>("SystemFillColorCriticalBrush");
        _relayUrlError.Visibility = Visibility.Collapsed;
        _relayToken = new PasswordBox { PlaceholderText = "粘贴中转生成的设备令牌" };
        Microsoft.UI.Xaml.Automation.AutomationProperties.SetName(_relayToken, "设备令牌");
        var saved = Text("已保存", "CaptionTextStyle");
        saved.Foreground = Resource<Brush>("SystemFillColorSuccessBrush");
        _relayTokenSaved = new StackPanel
        {
            Orientation = Orientation.Horizontal, Spacing = 6, Visibility = Visibility.Collapsed,
            Children = { new Ellipse { Width = 8, Height = 8, Fill = Resource<Brush>("SystemFillColorSuccessBrush") }, saved }
        };
        _relaySave = ActionButton("保存并连接", () => _ = SaveRelayAsync(), accent: true);
        _relayUrl.TextChanged += (_, _) => { _relayUrlTouched = true; ValidateRelayUrl(); };
        var form = Card(new StackPanel
        {
            Spacing = 16,
            Children =
            {
                new StackPanel { Spacing = 4, Children = { _relayUrl, _relayUrlError } },
                new StackPanel { Spacing = 8, Children = { PageControlRow(Text("设备令牌", "BodyTextStyle"), _relayTokenSaved), _relayToken } },
                PageControlRow(_relaySave, Text("仅主动外连中转，不开放任何入站端口", "CaptionTextStyle"))
            }
        });
        form.Padding = new Thickness(20);
        page.Children.Add(form);
        var host = Facts(("中转主机", " ", false));
        _relayHost = FactValues(host)["中转主机"];
        _relayUptimeRow = Facts(("在线时长", " ", false));
        _relayDuration = FactValues(_relayUptimeRow)["在线时长"];
        var config = Facts(("配置文件", " ", true));
        _relayConfig = FactValues(config)["配置文件"];
        page.Children.Add(Card(new StackPanel
        {
            Spacing = 12,
            Children =
            {
                host, _relayUptimeRow,
                PageControlRow(config, PageGlyphButton("打开", "\uE8DA", () => OpenPath(Json.Str(_paths, "config"), select: true))),
                Text("同一台电脑同一时间只能有一个 winhand 连着中转；终端里运行 winhand connect 时，这里会显示「有另一个实例」。", "CaptionTextStyle")
            }
        }));
        RenderRelayStatus(CurrentStatus);
        return PageScroll(page);
    }

    private void ValidateRelayUrl()
    {
        var url = _relayUrl!.Text.Trim();
        var invalid = url.Length > 0 && !url.StartsWith("ws://", StringComparison.OrdinalIgnoreCase) && !url.StartsWith("wss://", StringComparison.OrdinalIgnoreCase);
        _relayUrlError!.Visibility = invalid ? Visibility.Visible : Visibility.Collapsed;
        _relaySave!.IsEnabled = !invalid;
    }

    private void RenderRelayStatus(StatusView view)
    {
        if (_relayBadge is null)
            return;
        _relayBadge.Content = view.Label;
        _relayBadge.Style = Resource<Style>(view.Tone switch
        {
            Tone.Success => "StatusBadgeSuccessStyle",
            Tone.Active => "StatusBadgeActiveStyle",
            Tone.Warning => "StatusBadgeWarningStyle",
            Tone.Error => "StatusBadgeCriticalStyle",
            _ => "StatusBadgeNeutralStyle"
        });
        _relayError!.Visibility = Json.Str(_status, "state") is "refused" or "replaced" or "conflict" ? Visibility.Visible : Visibility.Collapsed;
        _relayError.Title = view.Label;
        _relayError.Message = view.Detail;
        _relayHost!.Text = view.Online ? Json.Str(_status, "relay") ?? "" : view.Label;
        _relayUptimeRow!.Visibility = view.Online ? Visibility.Visible : Visibility.Collapsed;
        _relayDuration!.Text = view.Online ? Format.Since(Json.Num(_status, "since")) : "";
        _relayConfig!.Text = Json.Str(_paths, "config") ?? "";
    }

    private UIElement BuildSettings()
    {
        var page = PagePanel();
        page.Spacing = 20;
        var heading = Text("设置", "PageTitleStyle");
        heading.Margin = new Thickness(0, 0, 0, -4);
        page.Children.Add(heading);

        _autostart = CompactSwitch("登录 Windows 后自动启动", Autostart.IsEnabled);
        _autostart.Toggled += (_, _) =>
        {
            try
            {
                Autostart.Set(_autostart.IsOn);
                RenderSettings();
            }
            catch (Exception error)
            {
                ShowNotice("无法修改开机启动", error.Message, InfoBarSeverity.Error);
            }
        };
        _autostartHint = Text("", "CaptionTextStyle");
        _autostartWarning = new InfoBar { Severity = InfoBarSeverity.Warning, IsClosable = false, IsOpen = true, Visibility = Visibility.Collapsed };
        page.Children.Add(LocalSettingsGroup("启动与托盘",
            new ContentControl
            {
                Style = Resource<Style>("SettingsRowCardStyle"),
                Content = PageControlRow(new StackPanel { Spacing = 4, Children = { Text("登录 Windows 后自动启动", "RowTitleStyle"), _autostartHint } }, _autostart)
            },
            _autostartWarning,
            Text("关闭窗口只会隐藏到通知区域；要彻底退出，右键托盘图标选“退出 winhand”。", "CaptionTextStyle")));

        page.Children.Add(LocalSettingsGroup("软件更新", BuildUpdateCard()));

        _configPath = Text("", "MonoDataStyle");
        _logsPath = Text("", "MonoDataStyle");
        _activityPath = Text("", "MonoDataStyle");
        var data = new StackPanel();
        foreach (var (title, key, path) in new[] { ("配置文件", "config", _configPath), ("日志", "logs", _logsPath), ("活动记录", "activity", _activityPath) })
        {
            path.TextWrapping = TextWrapping.NoWrap;
            path.TextTrimming = TextTrimming.CharacterEllipsis;
            if (data.Children.Count > 0)
                data.Children.Add(Divider());
            var row = PageControlRow(new StackPanel { Spacing = 2, Children = { Text(title, "RowTitleStyle"), path } },
                Named(PageGlyphButton("打开", "\uE8DA", () => OpenPath(Json.Str(_paths, key), select: key == "config")), $"打开{title}"));
            row.Padding = new Thickness(16, 10, 16, 10);
            data.Children.Add(row);
        }
        page.Children.Add(LocalSettingsGroup("本机数据", new ContentControl { Style = Resource<Style>("SettingsRowCardStyle"), Padding = new Thickness(0), Content = data }));

        _aboutText = Text("", "MonoDataStyle");
        _aboutText.IsTextSelectionEnabled = true;
        var repository = new HyperlinkButton { Content = "GitHub 项目主页", NavigateUri = new Uri(AppUpdater.RepositoryUrl), Padding = new Thickness(0) };
        page.Children.Add(LocalSettingsGroup("关于", new ContentControl { Style = Resource<Style>("SettingsRowCardStyle"), Content = PageControlRow(_aboutText, repository) }));

        RenderSettings();
        return PageScroll(page);
    }

    private static StackPanel LocalSettingsGroup(string title, params UIElement[] elements)
    {
        var rows = new StackPanel { Spacing = 4 };
        foreach (var element in elements)
            rows.Children.Add(element);
        return new StackPanel { Spacing = 8, Children = { Text(title, "SectionTitleStyle"), rows } };
    }

    private void RenderSettings()
    {
        if (_relayUrl is null)
            return;
        if (!_relayUrlTouched || _relayUrl.Text.Length == 0)
        {
            _relayUrl.Text = Json.Str(_relay, "url") ?? "";
            _relayUrlTouched = false;
        }
        _relayToken!.PlaceholderText = Json.Bool(_relay, "has_token") ? "已保存；留空保持不变" : "粘贴中转生成的设备令牌";
        _relayTokenSaved!.Visibility = Json.Bool(_relay, "has_token") ? Visibility.Visible : Visibility.Collapsed;
        ValidateRelayUrl();
        _autostartHint!.Text = Autostart.IsStale
            ? "开机启动指向另一个位置的 winhand；重新打开这个开关即可修正。"
            : Autostart.IsEnabled ? "登录后在后台启动并自动连接中转，不弹出窗口。" : "关闭时需要手动打开 winhand，Claude 才能连上这台电脑。";
        _autostartWarning!.Message = _autostartHint.Text;
        _autostartWarning.Visibility = Autostart.IsStale ? Visibility.Visible : Visibility.Collapsed;
        _autostartHint.Visibility = Autostart.IsStale ? Visibility.Collapsed : Visibility.Visible;
        _configPath!.Text = Json.Str(_paths, "config") ?? "";
        _logsPath!.Text = Json.Str(_paths, "logs") ?? "";
        _activityPath!.Text = Json.Str(_paths, "activity") ?? "";
        ToolTipService.SetToolTip(_configPath, _configPath.Text);
        ToolTipService.SetToolTip(_logsPath, _logsPath.Text);
        ToolTipService.SetToolTip(_activityPath, _activityPath.Text);
        var app = System.Reflection.Assembly.GetExecutingAssembly().GetName().Version?.ToString(3) ?? "?";
        _aboutText!.Text = $"winhand 桌面端 {app} · 后端 {_backendVersion ?? "未运行"}\n后端程序：{_backend.Executable}";
    }

    private async Task SaveRelayAsync()
    {
        var parameters = new Dictionary<string, object?>
        {
            ["url"] = _relayUrl!.Text.Trim(),
            ["token"] = _relayToken!.Password.Trim()
        };
        if (await RequestAsync("set_relay", parameters) is { } snapshot)
        {
            _relayToken.Password = "";
            _relayUrlTouched = false;
            ApplySnapshot(snapshot);
            RefreshStatus();
            ShowNotice("已保存", "正在用新的设置连接中转。", InfoBarSeverity.Success);
        }
    }
}
