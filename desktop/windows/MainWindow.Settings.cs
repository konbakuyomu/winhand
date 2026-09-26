using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace Winhand.Desktop;

public sealed partial class MainWindow
{
    private TextBox? _relayUrl;
    private PasswordBox? _relayToken;
    private ToggleSwitch? _autostart;
    private TextBlock? _pathsText, _aboutText, _autostartHint;
    private bool _relayUrlTouched;

    private UIElement BuildSettings()
    {
        var page = new StackPanel { Spacing = 16, Padding = new Thickness(24), MaxWidth = 900, HorizontalAlignment = HorizontalAlignment.Left };

        _relayUrl = new TextBox { Header = "中转地址", PlaceholderText = "wss://winhand.example.com/agent" };
        _relayUrl.TextChanged += (_, _) => _relayUrlTouched = true;
        _relayToken = new PasswordBox { Header = "设备令牌", PlaceholderText = "粘贴中转生成的设备令牌" };
        var save = ActionButton("保存并连接", () => _ = SaveRelayAsync(), accent: true);
        page.Children.Add(Card(new StackPanel
        {
            Spacing = 12,
            Children =
            {
                Text("中转", "SectionHeadingStyle"),
                Text("这台电脑主动连出到你的 Cloudflare 中转，不开放任何入站端口。令牌保存在本机 config.toml。", "SecondaryCopyStyle"),
                _relayUrl,
                _relayToken,
                save
            }
        }));

        _autostart = new ToggleSwitch { Header = "登录 Windows 后自动启动", OnContent = "开启（在通知区域运行）", OffContent = "关闭" };
        _autostart.IsOn = Autostart.IsEnabled;
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
        page.Children.Add(Card(new StackPanel
        {
            Spacing = 8,
            Children =
            {
                Text("启动", "SectionHeadingStyle"),
                _autostart,
                _autostartHint,
                Text("关闭窗口只会隐藏到通知区域；要彻底退出，右键托盘图标选“退出 winhand”。", "SecondaryCopyStyle")
            }
        }));

        _pathsText = Text("", "DataCopyStyle");
        var buttons = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        buttons.Children.Add(ActionButton("打开配置文件", () => OpenPath(Json.Str(_paths, "config"))));
        buttons.Children.Add(ActionButton("打开日志目录", () => OpenPath(Json.Str(_paths, "logs"))));
        buttons.Children.Add(ActionButton("打开活动记录", () => OpenPath(Json.Str(_paths, "activity"))));
        page.Children.Add(Card(new StackPanel
        {
            Spacing = 8,
            Children =
            {
                Text("本机数据", "SectionHeadingStyle"),
                Text("本机的其他 MCP 服务（pyocd、usb-camera 等）在“MCP 服务”页管理，也保存在 config.toml 里。", "SecondaryCopyStyle"),
                _pathsText,
                buttons
            }
        }));

        page.Children.Add(BuildUpdateCard());

        _aboutText = Text("", "SecondaryCopyStyle");
        page.Children.Add(Card(new StackPanel { Spacing = 8, Children = { Text("关于", "SectionHeadingStyle"), _aboutText } }));

        RenderSettings();
        return new ScrollViewer { Content = page };
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
            ? "开机启动指向的是另一个位置的 winhand，重新打开此开关可修正。"
            : Autostart.IsEnabled ? "登录后在后台启动并自动连接中转，不会弹出窗口。" : "";
        _pathsText!.Text = $"配置：{Json.Str(_paths, "config")}\n日志：{Json.Str(_paths, "logs")}\n活动：{Json.Str(_paths, "activity")}";
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
