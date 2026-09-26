using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace Winhand.Desktop;

/// <summary>The "概览" page: is Claude connected to this machine, what happened today, what
/// happened last. One connection panel, then the recent activity.</summary>
public sealed partial class MainWindow
{
    private ContentControl? _statusPill;
    private TextBlock? _statusDetail, _statusRelay, _todayFacts;
    private Button? _reconnectButton, _disconnectButton;
    private StackPanel? _recentRows;
    private ContentControl? _setupHint;

    private UIElement BuildOverview()
    {
        var page = PagePanel();

        _statusPill = Pill("启动中", Tone.Neutral);
        _statusDetail = Text("");
        _statusRelay = Text("", "DataCopyStyle");
        _todayFacts = Text("", "SecondaryCopyStyle");
        _reconnectButton = ActionButton("重新连接", () => _ = ReconnectAsync(), accent: true);
        _disconnectButton = ActionButton("断开", () => _ = DisconnectAsync());
        var statusText = new StackPanel { Spacing = 4 };
        statusText.Children.Add(new StackPanel
        {
            Orientation = Orientation.Horizontal,
            Spacing = 12,
            Children = { Text("中转连接", "SectionHeadingStyle"), _statusPill }
        });
        statusText.Children.Add(_statusDetail);
        statusText.Children.Add(_statusRelay);
        var buttons = ActionRow(_reconnectButton, _disconnectButton);
        buttons.VerticalAlignment = VerticalAlignment.Top;
        var statusGrid = new Grid { ColumnSpacing = 16 };
        statusGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        statusGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        statusGrid.Children.Add(statusText);
        Grid.SetColumn(buttons, 1);
        statusGrid.Children.Add(buttons);
        page.Children.Add(RowsPanel(statusGrid, _todayFacts));

        _setupHint = Card(new StackPanel
        {
            Spacing = 12,
            Children =
            {
                new StackPanel
                {
                    Spacing = 4,
                    Children =
                    {
                        Text("还没有配置中转", "SectionHeadingStyle"),
                        Text("填写中转地址（wss://…/agent）和设备令牌后，claude.ai 的 winhand 连接器就能操作这台电脑。", "SecondaryCopyStyle")
                    }
                },
                ActionRow(ActionButton("去设置", () => ShowPage("settings"), accent: true))
            }
        });
        _setupHint.Visibility = Visibility.Collapsed;
        page.Children.Add(_setupHint);

        _recentRows = new StackPanel { Spacing = 0 };
        var all = new HyperlinkButton { Content = "查看全部活动", Padding = new Thickness(0) };
        all.Click += (_, _) => ShowPage("activity");
        page.Children.Add(Section("最近活动", "Claude 最近调用的工具和连接变化。", Card(new StackPanel { Spacing = 8, Children = { _recentRows, all } })));
        return PageScroll(page);
    }

    private void RenderOverviewStatus(StatusView view)
    {
        if (_statusPill is null)
            return;
        _statusPill.Content = view.Label;
        _statusPill.Style = PillStyle(view.Tone);
        _statusDetail!.Text = view.Detail;
        _statusRelay!.Text = Json.Str(_relay, "url") ?? "";
        _statusRelay.Visibility = _statusRelay.Text.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
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
        var failed = today.Count(a => a.Status == "error");
        var open = _sessions.Count(s => Json.Str(s, "state") != "exited");
        _todayFacts!.Text = $"今天 {today.Count} 次工具调用" + (failed > 0 ? $"，{failed} 次失败" : "") + $" · {open} 个会话进行中";
        _recentRows.Children.Clear();
        var recent = _activity.AsEnumerable().Reverse().Take(8).ToList();
        if (recent.Count == 0)
            _recentRows.Children.Add(Text("还没有活动。Claude 调用工具后会实时出现在这里。", "SecondaryCopyStyle"));
        for (var i = 0; i < recent.Count; i++)
        {
            if (i > 0)
                _recentRows.Children.Add(Divider());
            _recentRows.Children.Add(CompactRow(recent[i]));
        }
    }

    private UIElement CompactRow(ActivityEntry entry)
    {
        var row = new Grid { ColumnSpacing = 12, Padding = new Thickness(0, 8, 0, 8) };
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(64) });
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        var time = Text(Format.Local(entry.Time).ToString("HH:mm:ss"), "DataCopyStyle");
        time.Margin = new Thickness(0, 2, 0, 0);
        row.Children.Add(time);
        var middle = new StackPanel { Spacing = 2 };
        middle.Children.Add(new TextBlock { Text = entry.Title, Style = Resource<Style>("LabelCopyStyle"), TextTrimming = TextTrimming.CharacterEllipsis, TextWrapping = TextWrapping.NoWrap });
        var detail = entry.Kind == "tool" ? entry.ArgsLine : entry.Summary;
        if (detail.Length > 0)
            middle.Children.Add(new TextBlock { Text = detail, Style = Resource<Style>("SecondaryCopyStyle"), TextTrimming = TextTrimming.CharacterEllipsis, TextWrapping = TextWrapping.NoWrap });
        Grid.SetColumn(middle, 1);
        row.Children.Add(middle);
        var pill = Pill(entry.StatusLabel, entry.Tone);
        pill.VerticalAlignment = VerticalAlignment.Top;
        Grid.SetColumn(pill, 2);
        row.Children.Add(pill);
        return row;
    }
}
