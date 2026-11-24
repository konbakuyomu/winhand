using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Shapes;

namespace Winhand.Desktop;

/// <summary>The "概览" page: connection, today's telemetry and recent activity.</summary>
public sealed partial class MainWindow
{
    private Ellipse? _overviewStatusDot;
    private TextBlock? _overviewStatusLabel, _statusDetail;
    private Button? _reconnectButton, _disconnectButton, _overviewConfigure;
    private InfoBar? _overviewUpdate;
    private StackPanel? _recentRows;
    private (TextBlock Value, TextBlock Note, Button Button)[] _overviewMetrics = [];
    private Style? _overviewMutedMetricStyle;
    private string? _recentKey;

    private UIElement BuildOverview()
    {
        var page = PagePanel();
        page.Spacing = 20;
        page.Children.Add(Text("概览", "PageTitleStyle"));

        _overviewStatusDot = new Ellipse { Style = DotStyle(Tone.Neutral), Width = 10, Height = 10, VerticalAlignment = VerticalAlignment.Center };
        _overviewStatusLabel = Text("启动中", "SectionTitleStyle");
        _statusDetail = Text("", "CaptionTextStyle");
        _statusDetail.Margin = new Thickness(19, 0, 0, 0);
        var statusText = new StackPanel
        {
            Spacing = 4,
            Children =
            {
                new StackPanel { Orientation = Orientation.Horizontal, Spacing = 9, Children = { _overviewStatusDot, _overviewStatusLabel } },
                _statusDetail
            }
        };
        _reconnectButton = ActionButton("重新连接", () => _ = ReconnectAsync());
        _disconnectButton = ActionButton("断开", () => _ = DisconnectAsync());
        _disconnectButton.Style = Resource<Style>("GhostButtonStyle");
        _overviewConfigure = ActionButton("去配置中转", () => ShowPage("relay"), accent: true);
        var actions = new Grid { ColumnSpacing = 8, VerticalAlignment = VerticalAlignment.Center };
        actions.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        actions.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        actions.Children.Add(_disconnectButton);
        actions.Children.Add(_reconnectButton);
        actions.Children.Add(_overviewConfigure);
        var top = new Grid { ColumnSpacing = 16, Padding = new Thickness(18, 16, 18, 16) };
        top.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        top.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        top.Children.Add(statusText);
        Grid.SetColumn(actions, 1);
        top.Children.Add(actions);

        var metrics = new Grid();
        _overviewMutedMetricStyle = (Style)Microsoft.UI.Xaml.Markup.XamlReader.Load("<Style xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation' TargetType='TextBlock' BasedOn='{StaticResource MetricValueStyle}'><Setter Property='Foreground' Value='{ThemeResource TextFillColorTertiaryBrush}' /></Style>");
        _overviewMetrics = new (TextBlock, TextBlock, Button)[3];
        var labels = new[] { "今日工具调用", "MCP 服务", "活动会话" };
        var targets = new[] { "activity", "mcp", "sessions" };
        for (var i = 0; i < 3; i++)
        {
            metrics.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            var value = Text("0", "MetricValueStyle");
            var note = Text("", "CaptionTextStyle");
            var content = new StackPanel { Spacing = 2, Children = { Text(labels[i], "CaptionTextStyle"), value, note } };
            var target = targets[i];
            var button = ActionButton("", () => ShowPage(target));
            button.Style = Resource<Style>("GhostButtonStyle");
            button.Background = Resource<Brush>("WinhandTransparentBrush");
            button.HorizontalAlignment = HorizontalAlignment.Stretch;
            button.HorizontalContentAlignment = HorizontalAlignment.Stretch;
            button.Padding = new Thickness(18, 14, 18, 15);
            button.Content = content;
            Grid.SetColumn(button, i);
            metrics.Children.Add(button);
            if (i > 0)
            {
                var line = (Border)Microsoft.UI.Xaml.Markup.XamlReader.Load("<Border xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation' Width='1' Margin='0,14' HorizontalAlignment='Left' Background='{ThemeResource DividerStrokeColorDefaultBrush}' IsHitTestVisible='False' />");
                Grid.SetColumn(line, i);
                metrics.Children.Add(line);
            }
            _overviewMetrics[i] = (value, note, button);
        }
        var statusCard = Card(new StackPanel { Children = { top, Divider(), metrics } });
        statusCard.Padding = new Thickness(0);
        page.Children.Add(statusCard);

        var updateAction = ActionButton("查看更新", () => ShowPage("settings"));
        updateAction.Style = Resource<Style>("GhostButtonStyle");
        _overviewUpdate = new InfoBar
        {
            Severity = InfoBarSeverity.Informational, IsOpen = true, IsClosable = false,
            Visibility = Visibility.Collapsed, ActionButton = updateAction
        };
        page.Children.Add(_overviewUpdate);

        var all = ActionButton("", () => ShowPage("activity"));
        all.Style = (Style)Microsoft.UI.Xaml.Markup.XamlReader.Load("<Style xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation' TargetType='Button' BasedOn='{StaticResource GhostButtonStyle}'><Setter Property='Foreground' Value='{ThemeResource WinhandAccentTextBrush}' /></Style>");
        all.Content = new StackPanel
        {
            Orientation = Orientation.Horizontal, Spacing = 6,
            Children = { new TextBlock { Text = "查看全部" }, new FontIcon { Glyph = "\uE76C", FontSize = 11 } }
        };
        Named(all, "查看全部活动");
        var heading = new Grid();
        heading.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        heading.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        var recentTitle = Text("最近活动", "SectionTitleStyle");
        recentTitle.VerticalAlignment = VerticalAlignment.Center;
        heading.Children.Add(recentTitle);
        Grid.SetColumn(all, 1);
        heading.Children.Add(all);
        _recentRows = new StackPanel();
        var recentCard = Card(_recentRows);
        recentCard.Padding = new Thickness(4);
        page.Children.Add(new StackPanel { Spacing = 8, Children = { heading, recentCard } });
        return PageScroll(page);
    }

    private void RenderOverviewStatus(StatusView view)
    {
        if (_overviewStatusLabel is null)
            return;
        _overviewStatusLabel.Text = view.Label;
        _overviewStatusDot!.Style = DotStyle(view.Tone);
        _statusDetail!.Text = view.Online
            ? $"{Json.Str(_status, "relay")} · 已在线 {Format.Since(Json.Num(_status, "since"))}"
            : view.Detail;
        ToolTipService.SetToolTip(_statusDetail, Json.Str(_relay, "url") ?? "");
        var state = _backend.IsRunning ? Json.Str(_status, "state") : null;
        var configure = state is "unconfigured" or "refused";
        var online = state is "online" or "connecting";
        _disconnectButton!.IsEnabled = _backend.IsRunning && state is "online" or "connecting" or "offline" or "refused";
        _disconnectButton.Visibility = !configure && _disconnectButton.IsEnabled ? Visibility.Visible : Visibility.Collapsed;
        _reconnectButton!.IsEnabled = !_starting;
        _reconnectButton.Visibility = configure ? Visibility.Collapsed : Visibility.Visible;
        _reconnectButton.Style = Resource<Style>(online ? "DefaultButtonStyle" : "AccentButtonStyle");
        Grid.SetColumn(_disconnectButton, online ? 0 : 1);
        Grid.SetColumn(_reconnectButton, online ? 1 : 0);
        _overviewConfigure!.Content = state == "refused" ? "更新令牌" : "去配置中转";
        _overviewConfigure.Visibility = configure ? Visibility.Visible : Visibility.Collapsed;
        _overviewUpdate!.Visibility = _updater.Ready || _updater.Available ? Visibility.Visible : Visibility.Collapsed;
        _overviewUpdate.Title = _updater.Ready ? $"新版本 {_updater.LatestVersion} 已就绪" : $"发现新版本 {_updater.LatestVersion}";
    }

    private void RenderOverview()
    {
        if (_recentRows is null)
            return;
        var today = _activity.Where(a => a.Kind == "tool" && Format.Local(a.Time).Date == DateTime.Today).ToList();
        var failed = today.Count(a => a.Status == "error");
        _overviewMetrics[0].Value.Text = today.Count.ToString();
        _overviewMetrics[0].Note.Text = failed > 0 ? $"含 {failed} 次失败" : "全部成功";
        _overviewMetrics[0].Note.Style = Resource<Style>(failed > 0 ? "ErrorCopyStyle" : "CaptionTextStyle");
        _overviewMetrics[0].Note.FontSize = 12;
        Named(_overviewMetrics[0].Button, $"今日工具调用 {today.Count} 次，查看活动");
        var servers = McpServers;
        var running = servers.Count(s => McpState(s).Label == "运行中");
        var errors = servers.Count(s => McpState(s).Label == "出错");
        var serviceNotes = new List<string>();
        if (running > 0) serviceNotes.Add($"{running} 运行中");
        if (errors > 0) serviceNotes.Add($"{errors} 出错");
        _overviewMetrics[1].Value.Text = servers.Count.ToString();
        _overviewMetrics[1].Note.Text = servers.Count == 0 ? "未添加" : serviceNotes.Count > 0 ? string.Join(" · ", serviceNotes) : "全部待命";
        Named(_overviewMetrics[1].Button, $"MCP 服务 {servers.Count} 个，查看服务");
        var open = _sessions.Count(s => Json.Str(s, "state") != "exited");
        var attention = _sessions.Count(s => Json.Str(s, "state") is "needs_user" or "blocked");
        _overviewMetrics[2].Value.Text = open.ToString();
        _overviewMetrics[2].Value.Style = open == 0 ? _overviewMutedMetricStyle : Resource<Style>("MetricValueStyle");
        _overviewMetrics[2].Note.Text = open == 0 ? "无活跃会话" : attention > 0 ? $"{attention} 个要处理" : "进行中";
        _overviewMetrics[2].Button.IsEnabled = open > 0;
        Named(_overviewMetrics[2].Button, $"活动会话 {open} 个" + (open > 0 ? "，查看会话" : "，无活跃会话"));

        var recent = _activity.AsEnumerable().Reverse().Take(8).ToList();
        // Sessions report every second; rebuild only when the activity itself changes.
        var key = string.Join("|", recent.Select(a => a.Data.GetRawText()));
        if (key == _recentKey && _recentRows.Children.Count > 0)
            return;
        _recentKey = key;
        _recentRows.Children.Clear();
        if (recent.Count == 0)
        {
            var empty = new StackPanel { Spacing = 12, Padding = new Thickness(24), HorizontalAlignment = HorizontalAlignment.Center };
            empty.Children.Add((FontIcon)Microsoft.UI.Xaml.Markup.XamlReader.Load("<FontIcon xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation' Glyph='&#xE81C;' FontSize='24' Foreground='{ThemeResource TextFillColorTertiaryBrush}' />"));
            var message = Text("还没有活动。Claude 调用工具后会实时出现在这里。", "CaptionTextStyle");
            message.TextAlignment = TextAlignment.Center;
            empty.Children.Add(message);
            _recentRows.Children.Add(empty);
        }
        for (var i = 0; i < recent.Count; i++)
        {
            if (i > 0) _recentRows.Children.Add(Divider());
            _recentRows.Children.Add(CompactRow(recent[i]));
        }
    }

    private UIElement CompactRow(ActivityEntry entry)
    {
        var row = new Grid { ColumnSpacing = 12, Padding = new Thickness(12, 10, 12, 10) };
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(60) });
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(56) });
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(14) });
        var time = (TextBlock)Microsoft.UI.Xaml.Markup.XamlReader.Load("<TextBlock xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation' Style='{StaticResource MonoDataStyle}' Foreground='{ThemeResource TextFillColorTertiaryBrush}' />");
        time.Text = Format.Local(entry.Time).ToString("HH:mm:ss");
        time.Margin = new Thickness(0, 2, 0, 0);
        row.Children.Add(time);
        var middle = new StackPanel { Spacing = 2 };
        middle.Children.Add(new TextBlock { Text = entry.Title, Style = Resource<Style>(ActivityTitleStyleKey(entry.Title)), TextTrimming = TextTrimming.CharacterEllipsis, TextWrapping = TextWrapping.NoWrap });
        middle.Children.Add(new TextBlock { Text = entry.Kind == "tool" ? entry.ArgsLine : entry.Summary, Style = Resource<Style>("MonoDataStyle"), TextTrimming = TextTrimming.CharacterEllipsis, TextWrapping = TextWrapping.NoWrap });
        Grid.SetColumn(middle, 1);
        row.Children.Add(middle);
        var duration = Text(Format.Duration(entry.DurationMs), "MonoDataStyle");
        duration.HorizontalAlignment = HorizontalAlignment.Right;
        duration.VerticalAlignment = VerticalAlignment.Center;
        Grid.SetColumn(duration, 2);
        row.Children.Add(duration);
        var pill = Pill(entry.StatusLabel, entry.Tone);
        pill.HorizontalAlignment = HorizontalAlignment.Right;
        Grid.SetColumn(pill, 3);
        row.Children.Add(pill);
        var arrow = new FontIcon { Glyph = "\uE76C", FontSize = 11, Opacity = 0 };
        Grid.SetColumn(arrow, 4);
        row.Children.Add(arrow);
        var button = ActionButton("", () => ShowActivityEntry(entry.Id));
        button.Style = Resource<Style>("GhostButtonStyle");
        button.Background = Resource<Brush>("WinhandTransparentBrush");
        button.HorizontalAlignment = HorizontalAlignment.Stretch;
        button.HorizontalContentAlignment = HorizontalAlignment.Stretch;
        button.Padding = new Thickness(0);
        button.Content = row;
        Named(button, $"{entry.Title}，{entry.StatusLabel}，查看活动详情");
        var hovered = false;
        void Arrow() => arrow.Opacity = hovered || button.FocusState == FocusState.Keyboard ? 1 : 0;
        button.PointerEntered += (_, _) => { hovered = true; Arrow(); };
        button.PointerExited += (_, _) => { hovered = false; Arrow(); };
        button.GotFocus += (_, _) => Arrow();
        button.LostFocus += (_, _) => Arrow();
        return button;
    }
}
