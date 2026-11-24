using System.Globalization;
using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using Windows.System;

namespace Winhand.Desktop;

public sealed partial class MainWindow
{
    private sealed class ActivityRow(ListViewItem root, ContentControl pill, TextBlock duration, TextBlock detail, TextBlock title)
    {
        public ListViewItem Root { get; } = root;
        public ContentControl Pill { get; } = pill;
        public TextBlock Duration { get; } = duration;
        public TextBlock Detail { get; } = detail;
        public TextBlock Title { get; } = title;
    }

    private const int MaxActivity = 1000;
    private readonly List<ActivityEntry> _activity = [];
    private readonly Dictionary<string, ActivityEntry> _activityById = [];
    private readonly Dictionary<string, ActivityRow> _activityRows = [];
    private ListView? _activityList;
    private AutoSuggestBox? _activitySearch;
    private SelectorBar? _activityFilters;
    private TextBlock? _activityCount;
    private HyperlinkButton? _activityReset;
    private string _activityFilter = "all";
    private string? _selectedActivity;
    private StackPanel? _activityDetail, _activityEmpty;
    private Grid? _activityWorkspace, _activityInspector, _activityGrip;
    private ContentControl? _activityDetailHeader;
    private Thumb? _activityDivider;
    private KeyboardAccelerator? _activityEscape;
    private bool _renderingActivityList, _activityWide, _activityPreviewSelected;
    private double _activityDetailWidth;
    private double? _activityDrawerHeight;

    private static bool IsIdentifierTitle(string title) =>
        title.Length > 0 && title.All(c => c <= 0x7F || c == '·');

    private static string ActivityTitleStyleKey(string title) =>
        IsIdentifierTitle(title) ? "MonoTitleStyle" : "RowTitleStyle";

    private UIElement BuildActivity()
    {
        var root = new Grid();
        root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        _activityFilters = new SelectorBar();
        foreach (var (tag, label) in new[] { ("all", "全部"), ("tool", "工具调用"), ("connection", "连接"), ("error", "失败") })
            _activityFilters.Items.Add(new SelectorBarItem { Text = label, Tag = tag, IsSelected = tag == "all" });
        _activityFilters.SelectionChanged += (sender, _) =>
        {
            _activityFilter = sender.SelectedItem?.Tag as string ?? "all";
            RebuildActivityList();
        };
        _activitySearch = new AutoSuggestBox
        {
            PlaceholderText = "搜索工具名、参数或结果", MinWidth = 220, MaxWidth = 280,
            QueryIcon = new FontIcon { Glyph = "\uE721" }, VerticalAlignment = VerticalAlignment.Center
        };
        Named(_activitySearch, "搜索活动");
        _activitySearch.TextChanged += (_, _) => RebuildActivityList();
        var find = new KeyboardAccelerator { Key = VirtualKey.F, Modifiers = VirtualKeyModifiers.Control };
        find.Invoked += (_, args) => { _activitySearch.Focus(FocusState.Keyboard); args.Handled = true; };
        root.KeyboardAccelerators.Add(find);
        var openLog = ActionButton("", () => OpenPath(Json.Str(_paths, "activity")));
        openLog.Style = Resource<Style>("GhostButtonStyle");
        openLog.Content = new StackPanel
        {
            Orientation = Orientation.Horizontal, Spacing = 6,
            Children = { new FontIcon { Glyph = "\uE8DA", FontSize = 14 }, Text("审计日志", "BodyTextStyle") }
        };
        Named(openLog, "打开审计日志");
        var first = new Grid { ColumnSpacing = 12 };
        first.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        first.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        first.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        var title = Text("活动", "PageTitleStyle");
        title.VerticalAlignment = VerticalAlignment.Center;
        first.Children.Add(title);
        Grid.SetColumn(_activitySearch, 1);
        first.Children.Add(_activitySearch);
        Grid.SetColumn(openLog, 2);
        first.Children.Add(openLog);
        _activityCount = Text("共 0 条", "CaptionTextStyle");
        _activityCount.VerticalAlignment = VerticalAlignment.Center;
        _activityReset = new HyperlinkButton { Content = "重置", Padding = new Thickness(4, 0, 0, 0), Visibility = Visibility.Collapsed };
        _activityReset.Click += (_, _) => ResetActivityFilters();
        var count = ActionRow(_activityCount, _activityReset);
        count.VerticalAlignment = VerticalAlignment.Center;
        var second = new Grid { Margin = new Thickness(0, 8, 0, 0) };
        second.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        second.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        second.Children.Add(_activityFilters);
        Grid.SetColumn(count, 1);
        second.Children.Add(count);
        var toolbar = new StackPanel { Padding = new Thickness(24, 18, 24, 10), Children = { first, second } };
        var toolbarBorder = (Border)Microsoft.UI.Xaml.Markup.XamlReader.Load("<Border xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation' BorderThickness='0,0,0,1' BorderBrush='{ThemeResource DividerStrokeColorDefaultBrush}' />");
        toolbarBorder.Child = toolbar;
        root.Children.Add(toolbarBorder);

        var listHost = new Grid();
        // Rows are prepended and can change height when a tool completes. Keep the scroll
        // offset to avoid WinUI's KeepItemsInView re-anchoring failure in this live list.
        // Each row is a ListViewItem of its own, as in the other lists: a bare element as an item
        // is moved in and out of containers the ListView makes and recycles.
        _activityList = new ListView
        {
            SelectionMode = ListViewSelectionMode.Single, Padding = new Thickness(12, 8, 12, 8),
            ItemsPanel = Resource<ItemsPanelTemplate>("ActivityItemsPanel")
        };
        Named(_activityList, "活动记录");
        _activityList.SelectionChanged += (_, _) =>
        {
            if (_renderingActivityList) return;
            _selectedActivity = (_activityList.SelectedItem as FrameworkElement)?.Tag as string;
            RenderActivityDetail();
        };
        _activityEmpty = new StackPanel { Margin = new Thickness(24), HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center };
        listHost.Children.Add(_activityList);
        listHost.Children.Add(_activityEmpty);

        _activityDetail = new StackPanel { Spacing = 16 };
        var detailScroll = new ScrollViewer
        {
            Content = _activityDetail, Padding = new Thickness(24, 14, 24, 20),
            HorizontalScrollMode = ScrollMode.Disabled, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto
        };
        _activityDetailHeader = new ContentControl { HorizontalContentAlignment = HorizontalAlignment.Stretch, Padding = new Thickness(24, 8, 20, 10) };
        var header = (Border)Microsoft.UI.Xaml.Markup.XamlReader.Load("<Border xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation' BorderThickness='0,0,0,1' BorderBrush='{ThemeResource DividerStrokeColorDefaultBrush}' />");
        header.Child = _activityDetailHeader;
        _activityGrip = (Grid)Microsoft.UI.Xaml.Markup.XamlReader.Load("<Grid xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation' Height='12' BorderThickness='0,1,0,0' BorderBrush='{ThemeResource DividerStrokeColorDefaultBrush}'><Border Width='36' Height='4' CornerRadius='2' HorizontalAlignment='Center' VerticalAlignment='Center' Background='{ThemeResource TextFillColorTertiaryBrush}' /></Grid>");
        var grip = new Thumb
        {
            Height = 12, HorizontalAlignment = HorizontalAlignment.Stretch,
            Template = (ControlTemplate)Microsoft.UI.Xaml.Markup.XamlReader.Load("<ControlTemplate xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation' TargetType='Thumb'><Border Background='{ThemeResource WinhandTransparentBrush}' /></ControlTemplate>")
        };
        Named(grip, "调整详情抽屉高度");
        grip.DragDelta += (_, args) =>
        {
            _activityDrawerHeight = Math.Clamp((_activityDrawerHeight ?? _activityWorkspace!.ActualHeight * 0.46) - args.VerticalChange,
                160, Math.Max(160, _activityWorkspace!.ActualHeight - 120));
            ArrangeActivityWorkspace();
        };
        _activityGrip.Children.Add(grip);
        _activityInspector = (Grid)Microsoft.UI.Xaml.Markup.XamlReader.Load("<Grid xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation' Background='{ThemeResource CardBackgroundFillColorDefaultBrush}' />");
        _activityInspector.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        _activityInspector.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        _activityInspector.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        _activityInspector.Children.Add(_activityGrip);
        Grid.SetRow(header, 1);
        _activityInspector.Children.Add(header);
        Grid.SetRow(detailScroll, 2);
        _activityInspector.Children.Add(detailScroll);
        _activityEscape = new KeyboardAccelerator { Key = VirtualKey.Escape, IsEnabled = false };
        _activityEscape.Invoked += (_, args) => { CloseActivityDrawer(); args.Handled = true; };
        _activityInspector.KeyboardAccelerators.Add(_activityEscape);

        _activityWorkspace = new Grid();
        _activityWorkspace.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        _activityWorkspace.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(0) });
        _activityWorkspace.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(0) });
        _activityWorkspace.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        _activityWorkspace.RowDefinitions.Add(new RowDefinition { Height = new GridLength(0) });
        _activityDetailWidth = double.TryParse(Preferences.ReadString("split:activity-detail"), NumberStyles.Float, CultureInfo.InvariantCulture, out var saved)
            ? Math.Max(360, saved) : 360;
        _activityDivider = new Thumb { Style = Resource<Style>("PaneDividerStyle") };
        Named(_activityDivider, "调整活动详情宽度");
        _activityDivider.DragDelta += (_, args) =>
        {
            _activityDetailWidth = Math.Clamp(_activityWorkspace.ColumnDefinitions[2].ActualWidth - args.HorizontalChange,
                360, _activityWorkspace.ActualWidth - 388);
            ArrangeActivityWorkspace();
        };
        _activityDivider.DragCompleted += (_, _) => Preferences.WriteString("split:activity-detail", _activityDetailWidth.ToString(CultureInfo.InvariantCulture));
        _activityWorkspace.Children.Add(listHost);
        Grid.SetColumn(_activityDivider, 1);
        _activityWorkspace.Children.Add(_activityDivider);
        _activityWorkspace.Children.Add(_activityInspector);
        _activityWorkspace.SizeChanged += (_, _) =>
        {
            var wasWide = _activityWide;
            ArrangeActivityWorkspace();
            if (wasWide != _activityWide) RenderActivityDetail();
        };
        Grid.SetRow(_activityWorkspace, 1);
        root.Children.Add(_activityWorkspace);
        RenderActivityDetail();
        UpdateActivityCountAndEmpty();
        return root;
    }

    private void ArrangeActivityWorkspace()
    {
        if (_activityWorkspace is null || _activityWorkspace.ActualWidth <= 0) return;
        _activityWide = _activityWorkspace.ActualWidth >= 760;
        _activityWorkspace.ColumnDefinitions[0].MinWidth = _activityWide ? 380 : 0;
        _activityWorkspace.ColumnDefinitions[1].Width = new GridLength(_activityWide ? 8 : 0);
        _activityWorkspace.ColumnDefinitions[2].Width = new GridLength(_activityWide
            ? Math.Clamp(_activityDetailWidth, 360, _activityWorkspace.ActualWidth - 388) : 0);
        _activityWorkspace.RowDefinitions[1].Height = !_activityWide && _selectedActivity is not null
            ? new GridLength(Math.Min(_activityDrawerHeight ?? _activityWorkspace.ActualHeight * 0.46, _activityWorkspace.ActualHeight - 120))
            : new GridLength(0);
        Grid.SetColumn(_activityInspector!, _activityWide ? 2 : 0);
        Grid.SetRow(_activityInspector!, _activityWide ? 0 : 1);
        _activityInspector!.Visibility = _activityWide || _selectedActivity is not null ? Visibility.Visible : Visibility.Collapsed;
        _activityGrip!.Visibility = _activityWide ? Visibility.Collapsed : Visibility.Visible;
        _activityDivider!.Visibility = _activityWide ? Visibility.Visible : Visibility.Collapsed;
        _activityEscape!.IsEnabled = !_activityWide && _selectedActivity is not null;
    }

    private void CloseActivityDrawer()
    {
        _activityDrawerHeight = null;
        _activityList!.SelectedItem = null;
        _activityList.Focus(FocusState.Keyboard);
    }

    private void ResetActivityFilters()
    {
        _activityFilter = "all";
        _activitySearch!.Text = "";
        _activityFilters!.SelectedItem = _activityFilters.Items[0];
        RebuildActivityList();
    }

    private void ShowActivityEntry(string id)
    {
        if (!_activityById.TryGetValue(id, out var entry)) return;
        if (!Matches(entry)) ResetActivityFilters();
        ShowPage("activity");
        _activityList!.SelectedItem = _activityRows[id].Root;
        _activityList.ScrollIntoView(_activityRows[id].Root);
        RenderActivityDetail();
    }

    private void LoadActivity(IEnumerable<JsonElement> items)
    {
        _activity.Clear();
        _activityById.Clear();
        foreach (var item in items)
        {
            var entry = new ActivityEntry(item);
            if (_activityById.TryAdd(entry.Id, entry)) _activity.Add(entry);
        }
        RebuildActivityList();
        if (!_activityPreviewSelected && PreviewState("selected") && _activity.Count > 0)
        {
            _activityPreviewSelected = true;
            _activityList!.SelectedItem = _activityRows[_activity[^1].Id].Root;
        }
    }

    private void UpsertActivity(JsonElement data)
    {
        var id = Json.Str(data, "id") ?? "";
        if (_activityById.TryGetValue(id, out var existing))
        {
            existing.Update(data);
            if (_activityRows.TryGetValue(id, out var row)) UpdateRow(existing, row);
            else if (Matches(existing)) RebuildActivityList();
            if (_selectedActivity == id) RenderActivityDetail();
        }
        else
        {
            var entry = new ActivityEntry(data);
            _activityById[id] = entry;
            _activity.Add(entry);
            if (_activity.Count > MaxActivity)
            {
                var oldest = _activity[0];
                _activity.RemoveAt(0);
                _activityById.Remove(oldest.Id);
                if (_activityRows.Remove(oldest.Id, out var gone)) _activityList?.Items.Remove(gone.Root);
            }
            if (Matches(entry) && _activityList is not null)
            {
                var row = CreateRow(entry);
                _activityRows[id] = row;
                _activityList.Items.Insert(0, row.Root);
            }
        }
        UpdateActivityCountAndEmpty();
        RenderOverview();
    }

    private bool Matches(ActivityEntry entry)
    {
        var kindOk = _activityFilter switch
        {
            "tool" => entry.Kind == "tool",
            "connection" => entry.Kind == "connection",
            "error" => entry.IsError,
            _ => true
        };
        var query = _activitySearch?.Text.Trim() ?? "";
        return kindOk && (query.Length == 0 ||
            entry.Title.Contains(query, StringComparison.OrdinalIgnoreCase) ||
            entry.ArgsLine.Contains(query, StringComparison.OrdinalIgnoreCase) ||
            entry.Summary.Contains(query, StringComparison.OrdinalIgnoreCase));
    }

    private void RebuildActivityList()
    {
        if (_activityList is null) return;
        _renderingActivityList = true;
        _activityList.Items.Clear();
        _activityRows.Clear();
        foreach (var entry in _activity.AsEnumerable().Reverse().Where(Matches))
        {
            var row = CreateRow(entry);
            _activityRows[entry.Id] = row;
            _activityList.Items.Add(row.Root);
            if (entry.Id == _selectedActivity) _activityList.SelectedItem = row.Root;
        }
        _renderingActivityList = false;
        _selectedActivity = (_activityList.SelectedItem as FrameworkElement)?.Tag as string;
        RenderActivityDetail();
        UpdateActivityCountAndEmpty();
    }

    private void UpdateActivityCountAndEmpty()
    {
        if (_activityCount is null) return;
        var filtered = _activityFilter != "all" || _activitySearch!.Text.Trim().Length > 0;
        _activityCount.Text = filtered ? $"匹配 {_activityRows.Count} / {_activity.Count} 条" : $"共 {_activity.Count} 条";
        _activityReset!.Visibility = filtered ? Visibility.Visible : Visibility.Collapsed;
        _activityEmpty!.Visibility = _activityRows.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        _activityEmpty.Children.Clear();
        var empty = _activity.Count == 0
            ? EmptyState("还没有活动", "Claude 调用工具、连接状态变化时会实时出现在这里。")
            : EmptyState("没有匹配的活动", "试试其他工具名、参数或结果。", ActionButton("清除筛选", ResetActivityFilters));
        empty.HorizontalAlignment = HorizontalAlignment.Center;
        _activityEmpty.Children.Add(empty);
    }

    private ActivityRow CreateRow(ActivityEntry entry)
    {
        var grid = new Grid { ColumnSpacing = 10, Padding = new Thickness(12, 10, 12, 10) };
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(60) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(56) });
        var when = Format.Local(entry.Time);
        var time = new StackPanel();
        var clock = (TextBlock)Microsoft.UI.Xaml.Markup.XamlReader.Load("<TextBlock xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation' Style='{StaticResource MonoDataStyle}' Foreground='{ThemeResource TextFillColorTertiaryBrush}' />");
        clock.Text = when.ToString("HH:mm:ss");
        time.Children.Add(clock);
        if (when.Date != DateTime.Today) time.Children.Add(Text(when.ToString("MM-dd"), "CaptionTextStyle"));
        grid.Children.Add(time);
        var title = new TextBlock { TextWrapping = TextWrapping.NoWrap, TextTrimming = TextTrimming.CharacterEllipsis };
        var detail = new TextBlock { Style = Resource<Style>("MonoDataStyle"), TextWrapping = TextWrapping.NoWrap, TextTrimming = TextTrimming.CharacterEllipsis };
        var middle = new StackPanel { Spacing = 2, Children = { title, detail } };
        Grid.SetColumn(middle, 1);
        grid.Children.Add(middle);
        var duration = (TextBlock)Microsoft.UI.Xaml.Markup.XamlReader.Load("<TextBlock xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation' Style='{StaticResource MonoDataStyle}' Foreground='{ThemeResource TextFillColorTertiaryBrush}' />");
        duration.VerticalAlignment = VerticalAlignment.Center;
        duration.HorizontalAlignment = HorizontalAlignment.Right;
        Grid.SetColumn(duration, 2);
        grid.Children.Add(duration);
        var pill = Pill("", Tone.Neutral);
        pill.HorizontalAlignment = HorizontalAlignment.Right;
        Grid.SetColumn(pill, 3);
        grid.Children.Add(pill);
        var item = new ListViewItem { Content = grid, Tag = entry.Id, Padding = new Thickness(0), HorizontalContentAlignment = HorizontalAlignment.Stretch };
        var row = new ActivityRow(item, pill, duration, detail, title);
        UpdateRow(entry, row);
        return row;
    }

    private static void UpdateRow(ActivityEntry entry, ActivityRow row)
    {
        row.Title.Text = entry.Title;
        row.Title.Style = Resource<Style>(ActivityTitleStyleKey(entry.Title));
        row.Pill.Content = entry.StatusLabel;
        row.Pill.Style = PillStyle(entry.Tone);
        row.Duration.Text = entry.Status == "running"
            ? Format.Duration((DateTime.Now - Format.Local(entry.Time)).TotalMilliseconds) : Format.Duration(entry.DurationMs);
        var args = entry.ArgsLine;
        row.Detail.Text = entry.Kind == "tool"
            ? (entry.Summary.Length > 0 && entry.Status != "running" ? $"{args}  →  {entry.Summary}" : args) : entry.Summary;
        row.Detail.Visibility = row.Detail.Text.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
    }

    private void RefreshRunningDurations()
    {
        foreach (var entry in _activity.Where(a => a.Status == "running"))
        {
            if (_activityRows.TryGetValue(entry.Id, out var row)) UpdateRow(entry, row);
        }
    }

    private void RenderActivityDetail()
    {
        if (_activityDetail is null) return;
        ArrangeActivityWorkspace();
        _activityDetail.Children.Clear();
        _activityDetailHeader!.Content = null;
        if (_selectedActivity is null || !_activityById.TryGetValue(_selectedActivity, out var entry))
        {
            var hint = (TextBlock)Microsoft.UI.Xaml.Markup.XamlReader.Load("<TextBlock xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation' Text='选择一条记录查看参数和结果' Style='{StaticResource CaptionTextStyle}' Foreground='{ThemeResource TextFillColorTertiaryBrush}' />");
            hint.HorizontalAlignment = HorizontalAlignment.Center;
            hint.VerticalAlignment = VerticalAlignment.Center;
            var placeholder = new Grid();
            placeholder.Children.Add(hint);
            Grid.SetRowSpan(placeholder, 3);
            if (_activityInspector!.Children.Count > 3) _activityInspector.Children.RemoveAt(3);
            _activityInspector.Children.Add(placeholder);
            _activityDetailHeader.Visibility = Visibility.Collapsed;
            return;
        }
        if (_activityInspector!.Children.Count > 3) _activityInspector.Children.RemoveAt(3);
        _activityDetailHeader.Visibility = Visibility.Visible;
        var facts = $"{entry.KindLabel} · {Format.Local(entry.Time):yyyy-MM-dd HH:mm:ss}";
        if (entry.DurationMs is { } ms) facts += $" · 用时 {Format.Duration(ms)}";
        var title = Text(entry.Title, ActivityTitleStyleKey(entry.Title));
        title.FontSize = 16;
        title.TextWrapping = TextWrapping.NoWrap;
        title.TextTrimming = TextTrimming.CharacterEllipsis;
        var titleLine = new Grid { ColumnSpacing = 8 };
        titleLine.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        titleLine.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        titleLine.Children.Add(title);
        var pill = Pill(entry.StatusLabel, entry.Tone);
        Grid.SetColumn(pill, 1);
        titleLine.Children.Add(pill);
        var labels = new StackPanel { Spacing = 4, Children = { titleLine, Text(facts, "CaptionTextStyle") } };
        labels.SizeChanged += (_, _) => title.MaxWidth = Math.Max(0, labels.ActualWidth - pill.ActualWidth - 8);
        var copy = ActionButton("复制 JSON", () => CopyText(Json.Pretty(entry.Data), "已复制 JSON", ""));
        copy.Style = Resource<Style>("GhostButtonStyle");
        var close = ActionButton("", CloseActivityDrawer);
        close.Style = Resource<Style>("IconGhostButtonStyle");
        close.Content = new FontIcon { Glyph = "\uE711", FontSize = 14 };
        close.Visibility = _activityWide ? Visibility.Collapsed : Visibility.Visible;
        Named(close, "关闭活动详情");
        var actions = ActionRow(copy, close);
        var head = new Grid { ColumnSpacing = 8 };
        head.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        head.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        head.Children.Add(labels);
        Grid.SetColumn(actions, 1);
        head.Children.Add(actions);
        _activityDetailHeader.Content = head;
        var resultHead = new Grid();
        resultHead.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        resultHead.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        resultHead.Children.Add(Text("结果", "RowTitleStyle"));
        var copyResult = ActionButton("", () => CopyText(entry.Summary, "已复制结果", ""));
        copyResult.Style = Resource<Style>("IconGhostButtonStyle");
        copyResult.Content = new FontIcon { Glyph = "\uE8C8", FontSize = 14 };
        Named(copyResult, "复制结果");
        Grid.SetColumn(copyResult, 1);
        resultHead.Children.Add(copyResult);
        _activityDetail.Children.Add(new StackPanel { Spacing = 6, Children = { resultHead, CodeBlock(entry.Summary) } });
        _activityDetail.Children.Add(new StackPanel
        {
            Spacing = 6,
            Children = { Text("参数", "RowTitleStyle"), Text("长内容已截断；env、令牌等已脱敏", "CaptionTextStyle"), CodeBlock(Json.Pretty(entry.Args).Replace("\r\n", "\n")) }
        });
    }
}
