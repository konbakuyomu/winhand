using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace Winhand.Desktop;

public sealed partial class MainWindow
{
    private ContentControl? _statusPill;
    private TextBlock? _statusDetail, _statusRelay, _statToolsOk, _statToolsError, _statSessions;
    private Button? _reconnectButton, _disconnectButton;
    private StackPanel? _recentRows;
    private ContentControl? _setupHint;

    private UIElement BuildOverview()
    {
        var page = new StackPanel { Spacing = 16, Padding = new Thickness(24), MaxWidth = 1100, HorizontalAlignment = HorizontalAlignment.Stretch };

        // connection
        _statusPill = Pill("启动中", Tone.Neutral);
        _statusDetail = Text("", "BodyCopyStyle");
        _statusRelay = Text("", "SecondaryCopyStyle");
        _reconnectButton = ActionButton("重新连接", () => _ = ReconnectAsync(), accent: true);
        _disconnectButton = ActionButton("断开", () => _ = DisconnectAsync());
        var statusText = new StackPanel { Spacing = 8 };
        statusText.Children.Add(new StackPanel
        {
            Orientation = Orientation.Horizontal,
            Spacing = 12,
            Children = { Text("中转连接", "SectionHeadingStyle"), _statusPill }
        });
        statusText.Children.Add(_statusDetail);
        statusText.Children.Add(_statusRelay);
        var buttons = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8, VerticalAlignment = VerticalAlignment.Center };
        buttons.Children.Add(_reconnectButton);
        buttons.Children.Add(_disconnectButton);
        var statusGrid = new Grid { ColumnSpacing = 16 };
        statusGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        statusGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        statusGrid.Children.Add(statusText);
        Grid.SetColumn(buttons, 1);
        statusGrid.Children.Add(buttons);
        page.Children.Add(Card(statusGrid));

        _setupHint = Card(new StackPanel
        {
            Spacing = 8,
            Children =
            {
                Text("还没有配置中转", "SectionHeadingStyle"),
                Text("填写中转地址（wss://…/agent）和设备令牌后，claude.ai 的 winhand 连接器就能操作这台电脑。"),
                ActionButton("去设置", () => ShowPage("settings"), accent: true)
            }
        });
        _setupHint.Visibility = Visibility.Collapsed;
        page.Children.Add(_setupHint);

        // today's numbers
        var stats = new Grid { ColumnSpacing = 16 };
        for (var i = 0; i < 3; i++)
            stats.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        UIElement Stat(int column, string label, out TextBlock value)
        {
            value = new TextBlock { Text = "0", FontSize = 28, FontWeight = Microsoft.UI.Text.FontWeights.SemiBold };
            var card = Card(new StackPanel { Spacing = 4, Children = { Text(label, "SecondaryCopyStyle"), value } });
            Grid.SetColumn(card, column);
            return card;
        }
        stats.Children.Add(Stat(0, "今天成功的工具调用", out _statToolsOk));
        stats.Children.Add(Stat(1, "今天失败的工具调用", out _statToolsError));
        stats.Children.Add(Stat(2, "打开的会话", out _statSessions));
        page.Children.Add(stats);

        // recent activity
        _recentRows = new StackPanel { Spacing = 2 };
        var recentHeader = new Grid();
        recentHeader.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        recentHeader.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        recentHeader.Children.Add(Text("最近活动", "SectionHeadingStyle"));
        var all = new HyperlinkButton { Content = "查看全部" };
        all.Click += (_, _) => ShowPage("activity");
        Grid.SetColumn(all, 1);
        recentHeader.Children.Add(all);
        page.Children.Add(Card(new StackPanel { Spacing = 12, Children = { recentHeader, _recentRows } }));

        return new ScrollViewer { Content = page };
    }

    private void RenderOverviewStatus(StatusView view)
    {
        if (_statusPill is null)
            return;
        _statusPill.Content = view.Label;
        _statusPill.Style = PillStyle(view.Tone);
        _statusDetail!.Text = view.Detail;
        _statusRelay!.Text = Json.Str(_relay, "url") is { } url ? $"中转地址：{url}" : "";
        var state = Json.Str(_status, "state");
        _disconnectButton!.IsEnabled = _backend.IsRunning && state is "online" or "connecting" or "offline" or "refused";
        _reconnectButton!.IsEnabled = !_starting;
        _setupHint!.Visibility = state == "unconfigured" ? Visibility.Visible : Visibility.Collapsed;
    }

    private void RenderOverview()
    {
        if (_recentRows is null)
            return;
        var today = _activity.Where(a => a.Kind == "tool" && Format.Local(a.Time).Date == DateTime.Today).ToList();
        _statToolsOk!.Text = today.Count(a => a.Status == "ok").ToString();
        _statToolsError!.Text = today.Count(a => a.Status == "error").ToString();
        _statSessions!.Text = _sessions.Count(s => Json.Str(s, "state") != "exited").ToString();
        _recentRows.Children.Clear();
        var recent = _activity.AsEnumerable().Reverse().Take(8).ToList();
        if (recent.Count == 0)
            _recentRows.Children.Add(Text("还没有活动。Claude 调用工具后会实时出现在这里。", "SecondaryCopyStyle"));
        foreach (var entry in recent)
            _recentRows.Children.Add(CompactRow(entry));
    }

    private UIElement CompactRow(ActivityEntry entry)
    {
        var row = new Grid { ColumnSpacing = 12, Padding = new Thickness(0, 4, 0, 4) };
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(64) });
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        row.Children.Add(Text(Format.Local(entry.Time).ToString("HH:mm:ss"), "DataCopyStyle"));
        var middle = new StackPanel();
        middle.Children.Add(new TextBlock { Text = entry.Title, FontWeight = Microsoft.UI.Text.FontWeights.SemiBold, TextTrimming = TextTrimming.CharacterEllipsis });
        var detail = entry.Kind == "tool" ? entry.ArgsLine : entry.Summary;
        if (detail.Length > 0)
            middle.Children.Add(new TextBlock { Text = detail, Style = Resource<Style>("SecondaryCopyStyle"), TextTrimming = TextTrimming.CharacterEllipsis, TextWrapping = TextWrapping.NoWrap });
        Grid.SetColumn(middle, 1);
        row.Children.Add(middle);
        var pill = Pill(entry.StatusLabel, entry.Tone);
        Grid.SetColumn(pill, 2);
        row.Children.Add(pill);
        return row;
    }
}
