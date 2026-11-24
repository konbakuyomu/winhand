using System.Globalization;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Automation;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;
using Windows.System;

namespace Winhand.Desktop;

/// <summary>Shared page structure, following Smart Search's Windows layout: one 24 px content edge,
/// a 920 px reading width, sections (18 px heading, 12 px to content, 24 px apart), setting rows
/// with the description left and the control right, resizable list/detail splits, and operation
/// messages in a floating InfoBar so they never move the page.</summary>
public sealed partial class MainWindow
{
    private const double PageInset = 24;
    private const double ContentMaxWidth = 880;
    private Action? _feedbackAction;
    private string? _noticeTitle;
    private Microsoft.UI.Dispatching.DispatcherQueueTimer? _noticeTimer;
    private bool _uiErrorShown;

    // ------------------------------------------------------------------ pages

    private static StackPanel PagePanel() => new() { Spacing = PageInset, MaxWidth = ContentMaxWidth, HorizontalAlignment = HorizontalAlignment.Left };

    /// <summary>Development previews only (WINHAND_DESKTOP_INSTANCE set): whether WINHAND_DESKTOP_STATE, a
    /// comma-separated list, asks a page to show a state for its screenshot (revealed, selected, collapsed ...).</summary>
    private static bool PreviewState(string flag) =>
        !string.IsNullOrEmpty(Environment.GetEnvironmentVariable("WINHAND_DESKTOP_INSTANCE"))
        && (Environment.GetEnvironmentVariable("WINHAND_DESKTOP_STATE") ?? "").Split(',').Contains(flag);

    /// <summary>A vertically scrolling page whose content keeps the 24 px edge and fits the width.</summary>
    private static ScrollViewer PageScroll(FrameworkElement content, double inset = PageInset)
    {
        var scroll = new ScrollViewer
        {
            Content = content,
            Padding = new Thickness(inset, 20, inset, 28),
            HorizontalScrollMode = ScrollMode.Disabled,
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto
        };
        content.HorizontalAlignment = HorizontalAlignment.Left;
        void Fit()
        {
            if (scroll.ActualWidth > 0)
                content.Width = Math.Min(content.MaxWidth, Math.Max(0, scroll.ActualWidth - inset * 2));
        }
        scroll.SizeChanged += (_, _) => Fit();
        scroll.Loaded += (_, _) => Fit();
        return scroll;
    }

    private static Grid PageControlRow(FrameworkElement leading, FrameworkElement trailing)
    {
        var row = new Grid { ColumnSpacing = 16 };
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        leading.VerticalAlignment = trailing.VerticalAlignment = VerticalAlignment.Center;
        Grid.SetColumn(trailing, 1);
        row.Children.Add(leading);
        row.Children.Add(trailing);
        return row;
    }

    private static Button PageGlyphButton(string label, string glyph, Action action, string style = "GhostButtonStyle")
    {
        var button = ActionButton(label, action);
        button.Style = Resource<Style>(style);
        button.Content = new StackPanel
        {
            Orientation = Orientation.Horizontal, Spacing = 6,
            Children =
            {
                new FontIcon { Glyph = glyph, FontFamily = new Microsoft.UI.Xaml.Media.FontFamily("Segoe Fluent Icons, Segoe MDL2 Assets"), FontSize = 14 },
                new TextBlock { Text = label, VerticalAlignment = VerticalAlignment.Center }
            }
        };
        AutomationProperties.SetName(button, label);
        return button;
    }

    private static Grid PageHelpButton(string title, string description)
    {
        var button = Named(new Button
        {
            Style = Resource<Style>("FieldHelpButtonStyle"),
            Content = new FontIcon { Glyph = "\uE897", FontFamily = new Microsoft.UI.Xaml.Media.FontFamily("Segoe Fluent Icons, Segoe MDL2 Assets"), FontSize = 14 }
        }, $"{title}帮助");
        var tip = new TeachingTip { Target = button, Title = title, Content = Text(description, "BodyTextStyle"), IsLightDismissEnabled = true };
        button.Click += (_, _) => tip.IsOpen = true;
        return new Grid { Children = { button, tip } };
    }

    private static StackPanel Section(string title, string subtitle, params UIElement[] content)
    {
        var section = new StackPanel { Spacing = 12 };
        var heading = new StackPanel { Spacing = 4, Children = { Text(title, "SectionHeadingStyle") } };
        if (subtitle.Length > 0)
            heading.Children.Add(Text(subtitle, "SecondaryCopyStyle"));
        section.Children.Add(heading);
        foreach (var child in content)
            section.Children.Add(child);
        return section;
    }

    /// <summary>Title and description on the left, one control on the right (stacked when narrow).</summary>
    private static Grid SettingRow(string title, string description, FrameworkElement? control) =>
        SettingRow(title, description.Length > 0 ? Text(description, "SecondaryCopyStyle") : null, control);

    private static Grid SettingRow(string title, TextBlock? description, FrameworkElement? control)
    {
        var label = new StackPanel { Spacing = 4, VerticalAlignment = VerticalAlignment.Center };
        label.Children.Add(Text(title, "LabelCopyStyle"));
        if (description is not null)
            label.Children.Add(description);
        var row = new Grid { ColumnSpacing = 20 };
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        row.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        row.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        row.Children.Add(label);
        if (control is null)
            return row;
        Grid.SetColumn(control, 1);
        control.VerticalAlignment = VerticalAlignment.Center;
        if (AutomationProperties.GetName(control).Length == 0)
            AutomationProperties.SetName(control, title);
        row.Children.Add(control);
        row.SizeChanged += (_, args) =>
        {
            var stacked = args.NewSize.Width < 460 && control is not ToggleSwitch;
            row.RowSpacing = stacked ? 8 : 0;
            Grid.SetColumn(control, stacked ? 0 : 1);
            Grid.SetRow(control, stacked ? 1 : 0);
            control.HorizontalAlignment = stacked ? HorizontalAlignment.Left : HorizontalAlignment.Right;
        };
        return row;
    }

    /// <summary>A key/value fact list (label column aligned across rows).</summary>
    private static Grid Facts(params (string Label, string Value, bool Mono)[] facts)
    {
        var grid = new Grid { ColumnSpacing = 16, RowSpacing = 8 };
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(96) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        var row = 0;
        foreach (var (label, value, mono) in facts)
        {
            if (value.Length == 0)
                continue;
            grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            var name = Text(label, "SecondaryCopyStyle");
            name.FontSize = 14;
            Grid.SetRow(name, row);
            grid.Children.Add(name);
            var text = mono ? Text(value, "DataCopyStyle") : Text(value);
            if (mono)
                text.FontSize = 13;
            text.IsTextSelectionEnabled = true;
            Grid.SetRow(text, row);
            Grid.SetColumn(text, 1);
            grid.Children.Add(text);
            row++;
        }
        return grid;
    }

    private static StackPanel ActionRow(params UIElement[] buttons)
    {
        var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        foreach (var button in buttons)
            row.Children.Add(button);
        return row;
    }

    private static Border Divider() => new() { Style = Resource<Style>("SectionDividerStyle") };

    /// <summary>A switch without WinUI's built-in On/Off label (the row's title says what it does).</summary>
    private static ToggleSwitch CompactSwitch(string name, bool value)
    {
        var toggle = new ToggleSwitch { IsOn = value, OnContent = "", OffContent = "", MinWidth = 0, Margin = new Thickness(0, 0, -12, 0) };
        AutomationProperties.SetName(toggle, name);
        return toggle;
    }

    /// <summary>Screen-reader (and UI automation) name for a control whose visible text repeats.</summary>
    private static T Named<T>(T element, string name) where T : DependencyObject
    {
        AutomationProperties.SetName(element, name);
        return element;
    }

    // ------------------------------------------------------------------ split view

    /// <summary>List on the left, detail on the right; the divider is draggable and keyboard
    /// adjustable, its position is remembered per page. A window too narrow for both shows the list, then the item
    /// clicked in it with a way back.</summary>
    private static Grid SplitWorkspace(string key, FrameworkElement leading, FrameworkElement detail,
        double initialWidth, double minimumWidth, double maximumWidth, double detailMinimum, ListView list, string backLabel)
    {
        const double dividerWidth = 8;
        var width = double.TryParse(Preferences.ReadString("split:" + key), NumberStyles.Float, CultureInfo.InvariantCulture, out var saved)
            ? Math.Clamp(saved, minimumWidth, maximumWidth) : initialWidth;
        var layout = new Grid();
        layout.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(width) });
        layout.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(dividerWidth) });
        layout.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        layout.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        var divider = new Thumb { Style = Resource<Style>("PaneDividerStyle") };
        var detailHost = new Grid();
        detailHost.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        detailHost.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        var back = new Button { Style = Resource<Style>("GhostButtonStyle") };
        back.Content = new StackPanel
        {
            Orientation = Orientation.Horizontal, Spacing = 8,
            Children = { new FontIcon { Glyph = "\uE72B", FontSize = 14 }, Text(backLabel) }
        };
        back.HorizontalAlignment = HorizontalAlignment.Left;
        back.Margin = new Thickness(14, 10, 14, 0);
        back.Visibility = Visibility.Collapsed;
        AutomationProperties.SetName(back, backLabel);
        detailHost.Children.Add(back);
        Grid.SetRow(detail, 1);
        detailHost.Children.Add(detail);
        AutomationProperties.SetName(divider, "调整分栏宽度");
        ToolTipService.SetToolTip(divider, "拖动调整宽度；方向键微调");
        layout.Children.Add(leading);
        Grid.SetColumn(divider, 1);
        layout.Children.Add(divider);
        layout.Children.Add(detailHost);
        var compact = false;
        var drilledIn = false;
        void Arrange()
        {
            if (layout.ActualWidth <= 0)
                return;
            compact = layout.ActualWidth < minimumWidth + detailMinimum + dividerWidth;
            layout.ColumnDefinitions[0].Width = compact ? new GridLength(1, GridUnitType.Star)
                : new GridLength(Math.Clamp(width, minimumWidth, Math.Min(maximumWidth, layout.ActualWidth - detailMinimum - dividerWidth)));
            layout.ColumnDefinitions[1].Width = new GridLength(compact ? 0 : dividerWidth);
            layout.ColumnDefinitions[2].Width = compact ? new GridLength(0) : new GridLength(1, GridUnitType.Star);
            Grid.SetColumn(detailHost, compact ? 0 : 2);
            leading.Visibility = compact && drilledIn ? Visibility.Collapsed : Visibility.Visible;
            detailHost.Visibility = compact && !drilledIn ? Visibility.Collapsed : Visibility.Visible;
            back.Visibility = compact ? Visibility.Visible : Visibility.Collapsed;
            divider.Visibility = compact ? Visibility.Collapsed : Visibility.Visible;
        }
        void Resize(double delta)
        {
            if (compact || layout.ActualWidth <= 0)
                return;
            width = Math.Clamp(layout.ColumnDefinitions[0].ActualWidth + delta, minimumWidth,
                Math.Min(maximumWidth, layout.ActualWidth - detailMinimum - dividerWidth));
            Arrange();
        }
        void Persist() => Preferences.WriteString("split:" + key, width.ToString(CultureInfo.InvariantCulture));
        divider.DragDelta += (_, args) => Resize(args.HorizontalChange);
        divider.DragCompleted += (_, _) => Persist();
        divider.KeyDown += (_, args) =>
        {
            if (args.Key is not (VirtualKey.Left or VirtualKey.Right))
                return;
            Resize(args.Key == VirtualKey.Left ? -16 : 16);
            Persist();
            args.Handled = true;
        };
        list.IsItemClickEnabled = true;
        list.ItemClick += (_, _) =>
        {
            if (!compact)
                return;
            drilledIn = true;
            Arrange();
        };
        back.Click += (_, _) =>
        {
            drilledIn = false;
            Arrange();
        };
        layout.SizeChanged += (_, _) => Arrange();
        return layout;
    }

    /// <summary>The list side of a split: an optional toolbar row above a ListView.</summary>
    private static Grid ListPane(FrameworkElement? toolbar, ListView list)
    {
        var pane = new Grid();
        pane.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        pane.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        if (toolbar is not null)
        {
            toolbar.Margin = new Thickness(14, 18, 14, 10);
            pane.Children.Add(toolbar);
        }
        list.Padding = new Thickness(12, toolbar is null ? 12 : 0, 12, 12);
        Grid.SetRow(list, 1);
        pane.Children.Add(list);
        return pane;
    }

    private static Style DotStyle(Tone tone) => Resource<Style>(tone switch
    {
        Tone.Success => "ConnectionReadyStyle",
        Tone.Active => "ConnectionActiveStyle",
        Tone.Warning => "ConnectionPendingStyle",
        Tone.Error => "ConnectionFailedStyle",
        _ => "ConnectionIdleStyle"
    });

    /// <summary>One list row: a status dot, a title and a secondary line.</summary>
    private static ListViewItem ListRow(string tag, string title, string subtitle, Tone tone)
    {
        var dot = new Microsoft.UI.Xaml.Shapes.Ellipse
        {
            Style = DotStyle(tone),
            Margin = new Thickness(0, 7, 0, 0),
            VerticalAlignment = VerticalAlignment.Top
        };
        var text = new StackPanel { Spacing = 2 };
        text.Children.Add(new TextBlock { Text = title, Style = Resource<Style>("LabelCopyStyle"), TextTrimming = TextTrimming.CharacterEllipsis, TextWrapping = TextWrapping.NoWrap });
        text.Children.Add(new TextBlock { Text = subtitle, Style = Resource<Style>("SecondaryCopyStyle"), TextTrimming = TextTrimming.CharacterEllipsis, TextWrapping = TextWrapping.NoWrap });
        var grid = new Grid { ColumnSpacing = 10 };
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.Children.Add(dot);
        Grid.SetColumn(text, 1);
        grid.Children.Add(text);
        var item = new ListViewItem { Content = grid, Tag = tag, Padding = new Thickness(12, 8, 12, 8), HorizontalContentAlignment = HorizontalAlignment.Stretch };
        AutomationProperties.SetName(item, $"{title}，{subtitle}");
        return item;
    }

    /// <summary>Change a row made by <see cref="ListRow"/> in place (a rebuild would flicker and lose focus).</summary>
    private static void UpdateListRow(ListViewItem item, string title, string subtitle, Tone tone)
    {
        if (item.Content is not Grid { Children: [Microsoft.UI.Xaml.Shapes.Ellipse dot, StackPanel { Children: [TextBlock head, TextBlock sub] }] })
            return;
        var style = DotStyle(tone);
        if (dot.Style != style)
            dot.Style = style;
        if (head.Text != title)
            head.Text = title;
        if (sub.Text != subtitle)
            sub.Text = subtitle;
        AutomationProperties.SetName(item, $"{title}，{subtitle}");
    }

    /// <summary>The value text of each row in a <see cref="Facts"/> grid, by label.</summary>
    private static Dictionary<string, TextBlock> FactValues(Grid facts)
    {
        var labels = new Dictionary<int, string>();
        var values = new Dictionary<string, TextBlock>();
        foreach (var child in facts.Children.OfType<TextBlock>())
        {
            if (Grid.GetColumn(child) == 0)
                labels[Grid.GetRow(child)] = child.Text;
        }
        foreach (var child in facts.Children.OfType<TextBlock>())
        {
            if (Grid.GetColumn(child) == 1 && labels.TryGetValue(Grid.GetRow(child), out var label))
                values[label] = child;
        }
        return values;
    }

    /// <summary>The detail side of a split: a scrolling page with the standard edge.</summary>
    private static ScrollViewer DetailPane(FrameworkElement content) => PageScroll(content);

    /// <summary>What a list side says when it has nothing: what goes here and how to add it.</summary>
    private static StackPanel EmptyState(string title, string body, params UIElement[] actions)
    {
        var panel = new StackPanel { Spacing = 12, MaxWidth = 520, HorizontalAlignment = HorizontalAlignment.Left };
        var heading = Text(title, "SectionHeadingStyle");
        heading.TextAlignment = TextAlignment.Center;
        heading.HorizontalAlignment = HorizontalAlignment.Center;
        panel.Children.Add(heading);
        var description = Text(body, "SecondaryCopyStyle");
        description.TextAlignment = TextAlignment.Center;
        description.HorizontalAlignment = HorizontalAlignment.Center;
        panel.Children.Add(description);
        if (actions.Length > 0)
        {
            var row = ActionRow(actions);
            row.HorizontalAlignment = HorizontalAlignment.Center;
            panel.Children.Add(row);
        }
        return panel;
    }

    // ------------------------------------------------------------------ feedback

    private void InitializeFeedback()
    {
        var hovered = false;
        _noticeTimer = DispatcherQueue.CreateTimer();
        _noticeTimer.Interval = TimeSpan.FromSeconds(4);
        _noticeTimer.IsRepeating = false;
        _noticeTimer.Tick += (_, _) => { if (!hovered) HideNotice(); };
        OverlayNotice.PointerEntered += (_, _) => { hovered = true; _noticeTimer.Stop(); };
        OverlayNotice.PointerExited += (_, _) =>
        {
            hovered = false;
            if (OverlayNotice.IsOpen && OverlayNotice.Severity is InfoBarSeverity.Success or InfoBarSeverity.Informational)
                _noticeTimer.Start();
        };
        OverlayNotice.Closed += (_, _) =>
        {
            OverlayNoticeHost.Visibility = Visibility.Collapsed;
            _noticeTimer.Stop();
            _noticeTitle = null;
            _feedbackAction = null;
        };
        NoticeAction.Click += (_, _) =>
        {
            var action = _feedbackAction;
            HideNotice();
            action?.Invoke();
        };
    }

    /// <summary>Show an operation message at the bottom right without moving page content.</summary>
    private void ShowNotice(string title, string message, InfoBarSeverity severity, string? actionText = null, Action? action = null, TimeSpan? duration = null)
    {
        _noticeTitle = title;
        OverlayNotice.Title = title;
        OverlayNotice.Message = message;
        OverlayNotice.Severity = severity;
        _feedbackAction = action;
        NoticeAction.Content = actionText ?? "";
        NoticeAction.Visibility = action is null ? Visibility.Collapsed : Visibility.Visible;
        OverlayNotice.IsOpen = true;
        OverlayNoticeHost.Visibility = Visibility.Visible;
        _noticeTimer!.Stop();
        if (severity is InfoBarSeverity.Success or InfoBarSeverity.Informational)
        {
            _noticeTimer.Interval = duration ?? TimeSpan.FromSeconds(4);
            _noticeTimer.Start();
        }
    }

    private void HideNotice()
    {
        _noticeTitle = null;
        _noticeTimer!.Stop();
        OverlayNotice.IsOpen = false;
        OverlayNoticeHost.Visibility = Visibility.Collapsed;
        _feedbackAction = null;
    }

    /// <summary>After the window recovered from an error (see <see cref="CrashGuard"/>): say so once per run.</summary>
    internal void ShowUiErrorNotice()
    {
        if (_uiErrorShown)
            return;
        _uiErrorShown = true;
        // not from inside the failed callback: the notice is a layout change of its own
        DispatcherQueue.TryEnqueue(() => ShowNotice("界面出了一个错误",
            "winhand 已接住它并继续运行，Claude 的连接不受影响。详情记在 desktop-app.log。",
            InfoBarSeverity.Warning, "打开日志", () => OpenPath(CrashGuard.LogPath, select: true)));
    }
}
