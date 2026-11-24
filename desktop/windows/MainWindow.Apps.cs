using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Automation;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;

namespace Winhand.Desktop;

/// <summary>The "程序授权" section of the settings page: whether the person is asked before the AI
/// clicks, types into or operates a program on the desktop, and which programs it may operate
/// (PROTOCOL.md: apps_get, apps_set_enabled, apps_allow, apps_revoke and the apps event).</summary>
public sealed partial class MainWindow
{
    private JsonElement? _apps;
    private ToggleSwitch? _appsAsk;
    private StackPanel? _appsAlways, _appsSession, _appsDenied, _appsDeniedGroup;
    private InfoBar? _appsError;
    private TextBlock? _appsAskHint;
    private ContentControl? _appsAlwaysCount, _appsSessionCount;
    private ComboBox? _appsPick;
    private bool _appsRendering, _previewAppRevoked;

    private UIElement BuildAppsSection()
    {
        var page = PagePanel();
        page.Spacing = 16;
        var heading = ActionRow(Text("程序授权", "PageTitleStyle"), PageHelpButton("程序授权",
            "AI 第一次点击、输入或操作某个程序前，电脑上会弹窗问你：拒绝、允许到 WinHand 重启、一直允许。截图和读取屏幕文字不受这里限制。" +
            "终端（命令提示符、PowerShell 窗口、Windows Terminal 等）默认不许 AI 打字，请让它用命令工具，那里有守卫规则把关。"));
        page.Children.Add(PageControlRow(heading, PageGlyphButton("desktop-apps.toml", "\uE8DA", () => OpenPath(Json.Str(_apps, "file"), select: true))));
        _appsError = new InfoBar
        {
            Severity = InfoBarSeverity.Error, IsClosable = false, IsOpen = true, Visibility = Visibility.Collapsed,
            Title = "授权文件有错",
            ActionButton = PageGlyphButton("打开文件", "\uE8DA", () => OpenPath(Json.Str(_apps, "file"), select: true))
        };
        page.Children.Add(_appsError);
        var ask = _appsAsk = CompactSwitch("AI 操作程序前先问我", true);
        ask.Toggled += async (_, _) =>
        {
            if (_appsRendering)
                return;
            if (await RequestAsync("apps_set_enabled", new Dictionary<string, object?> { ["enabled"] = ask.IsOn }) is { } result)
                ApplyApps(result);
            else
                RenderApps(); // not changed: the switch goes back to what the file says
        };
        _appsAskHint = Text("AI 第一次操作某个程序前，先在这台电脑上弹窗问你。", "CaptionTextStyle");
        var shield = new Border
        {
            Width = 36, Height = 36, CornerRadius = new CornerRadius(6), Background = Resource<Brush>("WinhandAccentSoftBgBrush"),
            Child = new FontIcon { Glyph = "\uEA18", FontFamily = new FontFamily("Segoe Fluent Icons, Segoe MDL2 Assets"), FontSize = 18, Foreground = Resource<Brush>("WinhandAccentTextBrush") }
        };
        var copy = new StackPanel { Spacing = 4, Children = { Text("AI 操作程序前先问我", "RowTitleStyle"), _appsAskHint } };
        var guard = new Grid { ColumnSpacing = 12 };
        guard.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        guard.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        guard.Children.Add(shield);
        Grid.SetColumn(copy, 1);
        guard.Children.Add(copy);
        page.Children.Add(new ContentControl { Style = Resource<Style>("SettingsRowCardStyle"), Content = PageControlRow(guard, ask) });

        _appsAlways = new StackPanel();
        _appsSession = new StackPanel();
        _appsDenied = new StackPanel();
        _appsPick = new ComboBox { IsEditable = true, PlaceholderText = "选择开着的程序，或输入 exe 名", HorizontalAlignment = HorizontalAlignment.Stretch };
        AutomationProperties.SetName(_appsPick, "要一直允许的程序");
        var allow = Named(ActionButton("允许", () => _ = AllowAppAsync(), accent: true), "一直允许这个程序");
        page.Children.Add(Card(new StackPanel { Spacing = 8, Children = { Text("提前加入一直允许", "RowTitleStyle"), PageControlRow(_appsPick, allow) } }));
        _appsAlwaysCount = new ContentControl { Content = "0", Style = Resource<Style>("StatusBadgeNeutralStyle") };
        _appsSessionCount = new ContentControl { Content = "0", Style = Resource<Style>("StatusBadgeNeutralStyle") };
        page.Children.Add(AppList("一直允许", "保存在授权文件里，重启后依然有效", _appsAlways, _appsAlwaysCount));
        page.Children.Add(AppList("允许到 WinHand 重启", "", _appsSession, _appsSessionCount));
        _appsDeniedGroup = AppList("刚被拒绝（冷却中）", "", _appsDenied);
        _appsDeniedGroup.Visibility = Visibility.Collapsed;
        page.Children.Add(_appsDeniedGroup);
        page.Children.Add(Text("截图和读屏文字不受这里限制；终端默认不许 AI 直接打字，请它用命令工具。", "CaptionTextStyle"));
        return PageScroll(page);
    }

    private static StackPanel AppList(string title, string description, StackPanel list, ContentControl? count = null)
    {
        var heading = ActionRow(Text(title, "SectionTitleStyle"));
        if (count is not null)
            heading.Children.Add(count);
        var card = Card(list);
        card.Padding = new Thickness(0);
        return new StackPanel { Spacing = 8, Children = { PageControlRow(heading, Text(description, "CaptionTextStyle")), card } };
    }

    private void ApplyApps(JsonElement? apps)
    {
        if (apps is not { ValueKind: JsonValueKind.Object })
            return;
        _apps = apps;
        RenderApps();
        if (!_previewAppRevoked && PreviewState("revoked") && Json.Arr(apps, "always").FirstOrDefault() is { ValueKind: JsonValueKind.Object } first)
        {
            _previewAppRevoked = true;
            _ = RevokeAppAsync(Json.Str(first, "exe")!, permanent: true);
        }
    }

    private void RenderApps()
    {
        if (_appsAsk is null || _apps is not { } apps)
            return;
        _appsRendering = true;
        _appsAsk.IsOn = Json.Bool(apps, "enabled");
        _appsRendering = false;
        _appsAskHint!.Text = _appsAsk.IsOn ? "AI 第一次操作某个程序前，先在这台电脑上弹窗问你。"
            : "已关闭：AI 可以直接操作程序；WinHand 自己的窗口和以管理员身份运行的程序仍然不能操作。";
        if (_appsAsk.IsOn)
            _appsAskHint.ClearValue(TextBlock.ForegroundProperty);
        else
            _appsAskHint.Foreground = Resource<Brush>("SystemFillColorCautionBrush");

        var always = Json.Arr(apps, "always").ToList();
        var session = Json.Arr(apps, "session").ToList();
        _appsAlwaysCount!.Content = always.Count.ToString();
        _appsSessionCount!.Content = session.Count.ToString();
        FillApps(_appsAlways!, always, entry =>
        {
            var since = Json.Str(entry, "since");
            return since is null ? "" : $"加入于 {(DateTimeOffset.TryParse(since, out var at) ? at.ToLocalTime().ToString("yyyy-MM-dd HH:mm:ss") : since)}";
        }, permanent: true);
        FillApps(_appsSession!, session, entry => $"{Format.Since(Json.Num(entry, "since"))}前允许", permanent: false);
        var denied = Json.Arr(apps, "denied").ToList();
        _appsDeniedGroup!.Visibility = denied.Count > 0 ? Visibility.Visible : Visibility.Collapsed;
        _appsDenied!.Children.Clear();
        foreach (var entry in denied)
        {
            if (_appsDenied.Children.Count > 0)
                _appsDenied.Children.Add(Divider());
            var seconds = Json.Num(entry, "left_s") ?? 0;
            var row = new StackPanel
            {
                Spacing = 4, Margin = new Thickness(16, 12, 16, 12),
                Children = { Text(Json.Str(entry, "exe") ?? "", "MonoTitleStyle"), Text(seconds < 60 ? "不到 1 分钟" : $"还要 {Math.Ceiling(seconds / 60)} 分钟", "CaptionTextStyle") }
            };
            _appsDenied.Children.Add(row);
        }

        var error = Json.Str(apps, "error");
        _appsError!.Message = string.IsNullOrEmpty(error) ? "" : $"现在对所有程序都会先问你。请修正或删除这个文件：{error}";
        _appsError.Visibility = string.IsNullOrEmpty(error) ? Visibility.Collapsed : Visibility.Visible;

        if (_appsPick!.IsDropDownOpen)
            return;
        var allowed = always.Select(entry => Json.Str(entry, "exe")).ToHashSet();
        var typed = _appsPick.Text;
        _appsPick.Items.Clear();
        foreach (var running in Json.Arr(apps, "running"))
        {
            if (Json.Str(running, "exe") is { } exe && !allowed.Contains(exe))
            {
                var title = Json.Str(running, "title");
                _appsPick.Items.Add(new ComboBoxItem { Content = string.IsNullOrEmpty(title) ? exe : $"{exe} · {title}", Tag = exe });
            }
        }
        _appsPick.Text = typed;
    }

    private void FillApps(StackPanel list, List<JsonElement> entries, Func<JsonElement, string> describe, bool permanent)
    {
        list.Children.Clear();
        if (entries.Count == 0)
        {
            var empty = Text(permanent ? "暂无一直允许的程序" : "本次运行暂无临时授权的程序", "CaptionTextStyle");
            empty.Margin = new Thickness(16, 12, 16, 12);
            list.Children.Add(empty);
            return;
        }
        foreach (var entry in entries)
        {
            var exe = Json.Str(entry, "exe") ?? "";
            if (list.Children.Count > 0)
            {
                var divider = Divider();
                divider.Margin = new Thickness(16, 0, 16, 0);
                list.Children.Add(divider);
            }
            var revoke = Named(ActionButton("撤销", () => _ = RevokeAppAsync(exe, permanent)), $"撤销 {exe} 的授权");
            revoke.Style = Resource<Style>("DangerGhostButtonStyle");
            var row = PageControlRow(new StackPanel { Spacing = 4, Children = { Text(exe, "MonoTitleStyle"), Text(describe(entry), "CaptionTextStyle") } }, revoke);
            row.Padding = new Thickness(16, 12, 16, 12);
            list.Children.Add(row);
        }
    }

    private async Task RefreshAppsAsync()
    {
        if (!_backend.IsRunning)
            return;
        try
        {
            ApplyApps(await _backend.CallAsync("apps_get"));
        }
        catch (Exception)
        {
            // the section keeps what it showed; the next apps event or visit brings it up to date
        }
    }

    private async Task AllowAppAsync()
    {
        var exe = _appsPick!.SelectedItem is ComboBoxItem { Tag: string chosen } ? chosen : (_appsPick.Text ?? "").Trim();
        if (exe.Length == 0)
        {
            ShowNotice("请选择或输入程序", "例如 notepad.exe。", InfoBarSeverity.Warning);
            return;
        }
        if (await RequestAsync("apps_allow", new Dictionary<string, object?> { ["exe"] = exe }) is not { } result)
            return;
        _appsPick.SelectedItem = null;
        _appsPick.Text = "";
        ApplyApps(result);
        var added = Json.Str(result, "added") ?? exe;
        var caution = Json.Str(result, "caution");
        ShowNotice("已允许", caution is null ? $"AI 现在可以直接操作 {added}，不再先问你。" : $"已允许 {added}。{caution}",
            caution is null ? InfoBarSeverity.Success : InfoBarSeverity.Warning);
    }

    private async Task RevokeAppAsync(string exe, bool permanent)
    {
        if (await RequestAsync("apps_revoke", new Dictionary<string, object?> { ["exe"] = exe }) is { } result)
        {
            ApplyApps(result);
            if (permanent)
                ShowNotice($"已撤销 {exe} 的授权", "AI 下次操作该程序前会重新弹窗询问。", InfoBarSeverity.Informational,
                    "恢复", async () =>
                    {
                        if (await RequestAsync("apps_allow", new Dictionary<string, object?> { ["exe"] = exe }) is not { } restored)
                            return;
                        ApplyApps(restored);
                        var caution = Json.Str(restored, "caution");
                        ShowNotice(string.IsNullOrEmpty(caution) ? $"已恢复 {exe} 的一直允许" : $"已恢复 {exe} 的一直允许。{caution}", "",
                            string.IsNullOrEmpty(caution) ? InfoBarSeverity.Success : InfoBarSeverity.Warning, duration: TimeSpan.FromSeconds(3));
                    }, TimeSpan.FromSeconds(8));
            else
                ShowNotice($"已撤销 {exe} 的临时授权", "", InfoBarSeverity.Success);
        }
    }
}
