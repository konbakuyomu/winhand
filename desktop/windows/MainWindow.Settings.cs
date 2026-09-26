using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace Winhand.Desktop;

/// <summary>The "设置" page: one scrolling column of sections (relay, startup, updates, local
/// data, about); each setting is a row with its description left and its control right.</summary>
public sealed partial class MainWindow
{
    private TextBox? _relayUrl;
    private PasswordBox? _relayToken;
    private ToggleSwitch? _autostart;
    private TextBlock? _configPath, _logsPath, _activityPath, _aboutText, _autostartHint;
    private bool _relayUrlTouched;

    private UIElement BuildSettings()
    {
        var page = PagePanel();

        _relayUrl = new TextBox { Header = "中转地址", PlaceholderText = "wss://winhand.example.com/agent" };
        _relayUrl.TextChanged += (_, _) => _relayUrlTouched = true;
        _relayToken = new PasswordBox { Header = "设备令牌", PlaceholderText = "粘贴中转生成的设备令牌" };
        var save = ActionButton("保存并连接", () => _ = SaveRelayAsync(), accent: true);
        page.Children.Add(Section("中转", "这台电脑主动连出到你的 Cloudflare 中转，不开放任何入站端口。令牌只保存在本机的 config.toml。",
            Card(new StackPanel { Spacing = 16, Children = { _relayUrl, _relayToken, ActionRow(save) } })));

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
        _autostartHint = Text("", "SecondaryCopyStyle");
        page.Children.Add(Section("启动", "关闭窗口只会隐藏到通知区域；要彻底退出，右键托盘图标选“退出 winhand”。",
            RowsPanel(SettingRow("登录 Windows 后自动启动", _autostartHint, _autostart))));

        page.Children.Add(Section("更新", "", BuildUpdateCard()));

        _configPath = Text("", "DataCopyStyle");
        _logsPath = Text("", "DataCopyStyle");
        _activityPath = Text("", "DataCopyStyle");
        page.Children.Add(Section("本机数据", "配置、日志和活动记录都只保存在这台电脑上。本机 MCP 服务在“MCP 服务”页管理，也保存在配置文件里。",
            RowsPanel(
                SettingRow("配置文件", _configPath, ActionButton("打开", () => OpenPath(Json.Str(_paths, "config")))),
                SettingRow("日志", _logsPath, ActionButton("打开", () => OpenPath(Json.Str(_paths, "logs")))),
                SettingRow("活动记录", _activityPath, ActionButton("打开", () => OpenPath(Json.Str(_paths, "activity")))))));

        _aboutText = Text("", "SecondaryCopyStyle");
        _aboutText.IsTextSelectionEnabled = true;
        var repository = new HyperlinkButton { Content = "GitHub 项目主页", NavigateUri = new Uri(AppUpdater.RepositoryUrl), Padding = new Thickness(0) };
        page.Children.Add(Section("关于", "", Card(new StackPanel { Spacing = 8, Children = { _aboutText, repository } })));

        RenderSettings();
        return PageScroll(page);
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
        _autostartHint!.Text = Autostart.IsStale
            ? "开机启动指向另一个位置的 winhand；重新打开这个开关即可修正。"
            : Autostart.IsEnabled ? "登录后在后台启动并自动连接中转，不弹出窗口。" : "关闭时需要手动打开 winhand，Claude 才能连上这台电脑。";
        _configPath!.Text = Json.Str(_paths, "config") ?? "";
        _logsPath!.Text = Json.Str(_paths, "logs") ?? "";
        _activityPath!.Text = Json.Str(_paths, "activity") ?? "";
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
