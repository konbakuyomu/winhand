using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace Winhand.Desktop;

public sealed partial class MainWindow
{
    private sealed class ActivityRow(Grid root, ContentControl pill, TextBlock duration, TextBlock detail)
    {
        public Grid Root { get; } = root;
        public ContentControl Pill { get; } = pill;
        public TextBlock Duration { get; } = duration;
        public TextBlock Detail { get; } = detail;
    }

    private const int MaxActivity = 1000;
    private readonly List<ActivityEntry> _activity = [];
    private readonly Dictionary<string, ActivityEntry> _activityById = [];
    private readonly Dictionary<string, ActivityRow> _activityRows = [];
    private ListView? _activityList;
    private TextBox? _activitySearch;
    private string _activityFilter = "all";
    private string? _selectedActivity;
    private StackPanel? _activityDetail;
    private TextBlock? _activityEmpty;

    private UIElement BuildActivity()
    {
        var root = new Grid();
        root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });

        var filters = new SelectorBar();
        foreach (var (tag, label) in new[] { ("all", "全部"), ("tool", "工具调用"), ("connection", "连接"), ("error", "失败") })
            filters.Items.Add(new SelectorBarItem { Text = label, Tag = tag, IsSelected = tag == "all" });
        filters.SelectionChanged += (sender, _) =>
        {
            _activityFilter = sender.SelectedItem?.Tag as string ?? "all";
            RebuildActivityList();
        };
        _activitySearch = new TextBox { PlaceholderText = "按工具名、参数或结果筛选", Width = 280 };
        Named(_activitySearch, "筛选活动");
        _activitySearch.TextChanged += (_, _) => RebuildActivityList();
        var openLog = ActionButton("打开审计日志", () => OpenPath(Json.Str(_paths, "activity")));
        var toolbar = new Grid { ColumnSpacing = 12, Padding = new Thickness(PageInset, 12, PageInset, 12) };
        toolbar.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        toolbar.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        toolbar.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        toolbar.Children.Add(filters);
        Grid.SetColumn(_activitySearch, 1);
        toolbar.Children.Add(_activitySearch);
        Grid.SetColumn(openLog, 2);
        toolbar.Children.Add(openLog);
        root.Children.Add(new Border
        {
            Child = toolbar,
            BorderThickness = new Thickness(0, 0, 0, 1),
            BorderBrush = Resource<Microsoft.UI.Xaml.Media.Brush>("DividerStrokeColorDefaultBrush")
        });

        var listHost = new Grid();
        _activityList = new ListView { SelectionMode = ListViewSelectionMode.Single, Padding = new Thickness(12, 8, 12, 8) };
        Named(_activityList, "活动记录");
        _activityList.SelectionChanged += (_, _) =>
        {
            _selectedActivity = (_activityList.SelectedItem as FrameworkElement)?.Tag as string;
            RenderActivityDetail();
        };
        _activityEmpty = Text("还没有活动。Claude 调用工具、连接状态变化时会实时出现在这里。", "SecondaryCopyStyle");
        _activityEmpty.Margin = new Thickness(PageInset);
        listHost.Children.Add(_activityList);
        listHost.Children.Add(_activityEmpty);

        _activityDetail = new StackPanel { Spacing = 12, MaxWidth = ContentMaxWidth };
        var body = SplitWorkspace("activity", listHost, PageScroll(_activityDetail), 620, 420, 1200, 360);
        Grid.SetRow(body, 1);
        root.Children.Add(body);
        RenderActivityDetail();
        return root;
    }

    private void LoadActivity(IEnumerable<JsonElement> items)
    {
        _activity.Clear();
        _activityById.Clear();
        foreach (var item in items)
        {
            var entry = new ActivityEntry(item);
            if (_activityById.TryAdd(entry.Id, entry))
                _activity.Add(entry);
        }
        RebuildActivityList();
    }

    private void UpsertActivity(JsonElement data)
    {
        var id = Json.Str(data, "id") ?? "";
        if (_activityById.TryGetValue(id, out var existing))
        {
            existing.Update(data);
            if (_activityRows.TryGetValue(id, out var row))
                UpdateRow(existing, row);
            else if (Matches(existing))
                RebuildActivityList();
            if (_selectedActivity == id)
                RenderActivityDetail();
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
                if (_activityRows.Remove(oldest.Id, out var gone))
                    _activityList?.Items.Remove(gone.Root);
            }
            if (Matches(entry) && _activityList is not null)
            {
                var row = CreateRow(entry);
                _activityRows[id] = row;
                _activityList.Items.Insert(0, row.Root);
                _activityEmpty!.Visibility = Visibility.Collapsed;
            }
        }
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
        if (_activityList is null)
            return;
        _activityList.Items.Clear();
        _activityRows.Clear();
        foreach (var entry in _activity.AsEnumerable().Reverse().Where(Matches))
        {
            var row = CreateRow(entry);
            _activityRows[entry.Id] = row;
            _activityList.Items.Add(row.Root);
            if (entry.Id == _selectedActivity)
                _activityList.SelectedItem = row.Root;
        }
        _activityEmpty!.Visibility = _activityRows.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
    }

    private ActivityRow CreateRow(ActivityEntry entry)
    {
        var grid = new Grid { ColumnSpacing = 12, Padding = new Thickness(4, 6, 4, 6), Tag = entry.Id };
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(76) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });

        var when = Format.Local(entry.Time);
        var time = new StackPanel();
        time.Children.Add(Text(when.ToString("HH:mm:ss"), "DataCopyStyle"));
        if (when.Date != DateTime.Today)
            time.Children.Add(Text(when.ToString("MM-dd"), "SecondaryCopyStyle"));
        grid.Children.Add(time);

        var middle = new StackPanel { Spacing = 2 };
        var titleLine = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        titleLine.Children.Add(new TextBlock { Text = entry.Title, FontWeight = Microsoft.UI.Text.FontWeights.SemiBold });
        if (entry.Kind != "tool")
            titleLine.Children.Add(Text(entry.KindLabel, "SecondaryCopyStyle"));
        middle.Children.Add(titleLine);
        var detail = new TextBlock
        {
            Style = Resource<Style>("SecondaryCopyStyle"),
            TextWrapping = TextWrapping.NoWrap,
            TextTrimming = TextTrimming.CharacterEllipsis
        };
        middle.Children.Add(detail);
        Grid.SetColumn(middle, 1);
        grid.Children.Add(middle);

        var duration = Text("", "DataCopyStyle");
        duration.VerticalAlignment = VerticalAlignment.Center;
        Grid.SetColumn(duration, 2);
        grid.Children.Add(duration);
        var pill = Pill("", Tone.Neutral);
        Grid.SetColumn(pill, 3);
        grid.Children.Add(pill);

        var row = new ActivityRow(grid, pill, duration, detail);
        UpdateRow(entry, row);
        return row;
    }

    private static void UpdateRow(ActivityEntry entry, ActivityRow row)
    {
        row.Pill.Content = entry.StatusLabel;
        row.Pill.Style = PillStyle(entry.Tone);
        row.Duration.Text = entry.Status == "running"
            ? Format.Duration((DateTime.Now - Format.Local(entry.Time)).TotalMilliseconds)
            : Format.Duration(entry.DurationMs);
        var args = entry.ArgsLine;
        row.Detail.Text = entry.Kind == "tool"
            ? (entry.Summary.Length > 0 && entry.Status != "running" ? $"{args}  →  {entry.Summary}" : args)
            : entry.Summary;
        row.Detail.Visibility = row.Detail.Text.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
    }

    private void RefreshRunningDurations()
    {
        foreach (var entry in _activity.Where(a => a.Status == "running"))
        {
            if (_activityRows.TryGetValue(entry.Id, out var row))
                UpdateRow(entry, row);
        }
    }

    private void RenderActivityDetail()
    {
        if (_activityDetail is null)
            return;
        _activityDetail.Children.Clear();
        if (_selectedActivity is null || !_activityById.TryGetValue(_selectedActivity, out var entry))
        {
            _activityDetail.Children.Add(EmptyState("活动详情", "在左边选择一条记录，查看它的参数和结果。"));
            return;
        }
        var facts = $"{entry.KindLabel} · {Format.Local(entry.Time):yyyy-MM-dd HH:mm:ss}";
        if (entry.DurationMs is { } ms)
            facts += $" · 用时 {Format.Duration(ms)}";
        _activityDetail.Spacing = PageInset;
        _activityDetail.Children.Add(DetailHeader(entry.Title, facts, Pill(entry.StatusLabel, entry.Tone), null));
        if (entry.Summary.Length > 0)
            _activityDetail.Children.Add(Section("结果", "", CodeBlock(entry.Summary)));
        if (entry.Args is { } args)
            _activityDetail.Children.Add(Section("参数", "预览会截断长内容；env、令牌等已隐藏。", CodeBlock(Json.Pretty(args).Replace("\r\n", "\n"))));
    }
}
