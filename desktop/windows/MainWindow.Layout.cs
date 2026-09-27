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
/// messages in a header flyout so they never move the page.</summary>
public sealed partial class MainWindow
{
    private const double PageInset = 24;
    private const double ContentMaxWidth = 920;
    private Action? _feedbackAction;

    // ------------------------------------------------------------------ pages

    private static StackPanel PagePanel() => new() { Spacing = PageInset, MaxWidth = ContentMaxWidth, HorizontalAlignment = HorizontalAlignment.Left };

    /// <summary>A vertically scrolling page whose content keeps the 24 px edge and fits the width.</summary>
    private static ScrollViewer PageScroll(FrameworkElement content, double inset = PageInset)
    {
        var scroll = new ScrollViewer
        {
            Content = content,
            Padding = new Thickness(inset),
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

    /// <summary>Rows inside one bordered panel, separated by hairlines.</summary>
    private static ContentControl RowsPanel(params UIElement[] rows)
    {
        var stack = new StackPanel { Spacing = 12 };
        for (var i = 0; i < rows.Length; i++)
        {
            if (i > 0)
                stack.Children.Add(Divider());
            stack.Children.Add(rows[i]);
        }
        return Card(stack);
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
    /// adjustable, its position is remembered per page, and narrow windows stack the two.</summary>
    private static Grid SplitWorkspace(string key, FrameworkElement leading, FrameworkElement detail,
        double initialWidth, double minimumWidth, double maximumWidth, double detailMinimum = 420)
    {
        const double dividerWidth = 8;
        var width = double.TryParse(Preferences.ReadString("split:" + key), NumberStyles.Float, CultureInfo.InvariantCulture, out var saved)
            ? Math.Clamp(saved, minimumWidth, maximumWidth) : initialWidth;
        var layout = new Grid();
        layout.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(width) });
        layout.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(dividerWidth) });
        layout.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        layout.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        layout.RowDefinitions.Add(new RowDefinition { Height = new GridLength(0) });
        var divider = new Thumb { Style = Resource<Style>("PaneDividerStyle") };
        AutomationProperties.SetName(divider, "调整分栏宽度");
        ToolTipService.SetToolTip(divider, "拖动调整宽度；方向键微调");
        layout.Children.Add(leading);
        Grid.SetColumn(divider, 1);
        layout.Children.Add(divider);
        layout.Children.Add(detail);
        var compact = false;
        void Arrange()
        {
            if (layout.ActualWidth <= 0)
                return;
            compact = layout.ActualWidth < minimumWidth + detailMinimum + dividerWidth;
            layout.ColumnDefinitions[0].Width = compact ? new GridLength(1, GridUnitType.Star)
                : new GridLength(Math.Clamp(width, minimumWidth, Math.Min(maximumWidth, layout.ActualWidth - detailMinimum - dividerWidth)));
            layout.ColumnDefinitions[1].Width = new GridLength(compact ? 0 : dividerWidth);
            layout.ColumnDefinitions[2].Width = compact ? new GridLength(0) : new GridLength(1, GridUnitType.Star);
            layout.RowDefinitions[0].Height = new GridLength(compact ? 0.4 : 1, GridUnitType.Star);
            layout.RowDefinitions[1].Height = compact ? new GridLength(0.6, GridUnitType.Star) : new GridLength(0);
            Grid.SetColumn(detail, compact ? 0 : 2);
            Grid.SetRow(detail, compact ? 1 : 0);
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
            toolbar.Margin = new Thickness(PageInset, PageInset, PageInset, 12);
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
        Tone.Warning or Tone.Active => "ConnectionPendingStyle",
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

    /// <summary>Header of a detail page: title with a status pill, optional control at the right.</summary>
    private static Grid DetailHeader(string title, string subtitle, ContentControl? pill, FrameworkElement? trailing)
    {
        var heading = new StackPanel { Spacing = 4 };
        var titleLine = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 12 };
        titleLine.Children.Add(new TextBlock { Text = title, Style = Resource<Style>("PageHeadingStyle"), TextWrapping = TextWrapping.NoWrap, TextTrimming = TextTrimming.CharacterEllipsis });
        if (pill is not null)
            titleLine.Children.Add(pill);
        heading.Children.Add(titleLine);
        if (subtitle.Length > 0)
            heading.Children.Add(Text(subtitle, "SecondaryCopyStyle"));
        var grid = new Grid { ColumnSpacing = 16 };
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        grid.Children.Add(heading);
        if (trailing is not null)
        {
            trailing.VerticalAlignment = VerticalAlignment.Center;
            Grid.SetColumn(trailing, 1);
            grid.Children.Add(trailing);
        }
        return grid;
    }

    /// <summary>What a list side says when it has nothing: what goes here and how to add it.</summary>
    private static StackPanel EmptyState(string title, string body, params UIElement[] actions)
    {
        var panel = new StackPanel { Spacing = 12, MaxWidth = 520, HorizontalAlignment = HorizontalAlignment.Left };
        panel.Children.Add(Text(title, "SectionHeadingStyle"));
        panel.Children.Add(Text(body, "SecondaryCopyStyle"));
        if (actions.Length > 0)
            panel.Children.Add(ActionRow(actions));
        return panel;
    }

    // ------------------------------------------------------------------ feedback

    private void InitializeFeedback()
    {
        FeedbackClear.Click += (_, _) => HideNotice();
        FeedbackAction.Click += (_, _) =>
        {
            FeedbackFlyout.Hide();
            _feedbackAction?.Invoke();
        };
    }

    /// <summary>Show an operation message in the header flyout (reachable again until cleared).</summary>
    private void ShowNotice(string title, string message, InfoBarSeverity severity, string? actionText = null, Action? action = null)
    {
        FeedbackTitle.Text = title;
        FeedbackMessage.Text = message;
        FeedbackIcon.Glyph = severity switch
        {
            InfoBarSeverity.Error or InfoBarSeverity.Warning => "",
            InfoBarSeverity.Success => "",
            _ => ""
        };
        FeedbackIcon.Foreground = severity switch
        {
            InfoBarSeverity.Error => Resource<Microsoft.UI.Xaml.Media.Brush>("SystemFillColorCriticalBrush"),
            InfoBarSeverity.Warning => Resource<Microsoft.UI.Xaml.Media.Brush>("SystemFillColorCautionBrush"),
            _ => Resource<Microsoft.UI.Xaml.Media.Brush>("TextFillColorPrimaryBrush")
        };
        _feedbackAction = action;
        FeedbackAction.Content = actionText ?? "";
        FeedbackAction.Visibility = action is null ? Visibility.Collapsed : Visibility.Visible;
        FeedbackButton.IsEnabled = true;
        ToolTipService.SetToolTip(FeedbackButton, title);
        if (FeedbackButton.XamlRoot is not null && IsWindowVisible(_hwnd))
            FeedbackFlyout.ShowAt(FeedbackButton);
    }

    private void HideNotice()
    {
        FeedbackFlyout.Hide();
        FeedbackTitle.Text = FeedbackMessage.Text = "";
        _feedbackAction = null;
        FeedbackButton.IsEnabled = false;
        ToolTipService.SetToolTip(FeedbackButton, "操作提示");
    }
}
