using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Automation;
using Microsoft.UI.Xaml.Controls;

namespace Winhand.Desktop;

/// <summary>The "程序授权" section of the settings page: whether the person is asked before the AI
/// clicks, types into or operates a program on the desktop, and which programs it may operate
/// (PROTOCOL.md: apps_get, apps_set_enabled, apps_allow, apps_revoke and the apps event).</summary>
public sealed partial class MainWindow
{
    private JsonElement? _apps;
    private ToggleSwitch? _appsAsk;
    private StackPanel? _appsAlways, _appsSession;
    private TextBlock? _appsError;
    private ComboBox? _appsPick;
    private bool _appsRendering;

    private UIElement BuildAppsSection()
    {
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
        _appsAlways = new StackPanel { Spacing = 8 };
        _appsSession = new StackPanel { Spacing = 8 };
        _appsError = Text("", "ErrorCopyStyle");
        _appsError.TextWrapping = TextWrapping.Wrap;
        _appsError.Visibility = Visibility.Collapsed;
        _appsPick = new ComboBox { IsEditable = true, PlaceholderText = "选择开着的程序，或输入 exe 名", MinWidth = 280 };
        AutomationProperties.SetName(_appsPick, "要一直允许的程序");
        var allow = Named(ActionButton("允许", () => _ = AllowAppAsync()), "一直允许这个程序");

        return Section("程序授权",
            "AI 第一次点击、输入或操作某个程序前，电脑上会弹窗问你：拒绝、允许到 WinHand 重启、一直允许。截图和读取屏幕文字不受这里限制。" +
            "终端（命令提示符、PowerShell 窗口、Windows Terminal 等）默认不许 AI 打字，请让它用命令工具，那里有守卫规则把关。",
            _appsError,
            RowsPanel(
                SettingRow("AI 操作程序前先问我", "关闭后不再问；WinHand 自己的窗口和以管理员身份运行的程序仍然不能操作。", _appsAsk),
                AppList("一直允许", "保存在授权文件里，重启后仍然有效。", _appsAlways),
                AppList("允许到 WinHand 重启", "只在这次运行的 WinHand 里有效。", _appsSession),
                SettingRow("加入一直允许", "提前允许，AI 操作它时就不再弹窗。", ActionRow(_appsPick, allow))));
    }

    private static StackPanel AppList(string title, string description, StackPanel list) => new()
    {
        Spacing = 8,
        Children = { Text(title, "LabelCopyStyle"), Text(description, "SecondaryCopyStyle"), list }
    };

    private void ApplyApps(JsonElement? apps)
    {
        if (apps is not { ValueKind: JsonValueKind.Object })
            return;
        _apps = apps;
        RenderApps();
    }

    private void RenderApps()
    {
        if (_appsAsk is null || _apps is not { } apps)
            return;
        _appsRendering = true;
        _appsAsk.IsOn = Json.Bool(apps, "enabled");
        _appsRendering = false;

        var always = Json.Arr(apps, "always").ToList();
        FillApps(_appsAlways!, always, entry => Json.Str(entry, "since") is { } since ? $"加入于 {since.Replace('T', ' ')}" : "");
        FillApps(_appsSession!, Json.Arr(apps, "session").ToList(), entry => $"{Format.Since(Json.Num(entry, "since"))}前允许");

        var error = Json.Str(apps, "error");
        _appsError!.Text = error is null ? "" : $"授权文件有错，现在对所有程序都会先问你。请修正或删除这个文件：{error}";
        _appsError.Visibility = error is null ? Visibility.Collapsed : Visibility.Visible;

        if (_appsPick!.IsDropDownOpen)
            return;
        var allowed = always.Select(entry => Json.Str(entry, "exe")).ToHashSet();
        var typed = _appsPick.Text;
        _appsPick.Items.Clear();
        foreach (var running in Json.Arr(apps, "running"))
        {
            if (Json.Str(running, "exe") is { } exe && !allowed.Contains(exe))
                _appsPick.Items.Add(exe);
        }
        _appsPick.Text = typed;
    }

    private void FillApps(StackPanel list, List<JsonElement> entries, Func<JsonElement, string> describe)
    {
        list.Children.Clear();
        if (entries.Count == 0)
        {
            list.Children.Add(Text("（无）", "SecondaryCopyStyle"));
            return;
        }
        foreach (var entry in entries)
        {
            var exe = Json.Str(entry, "exe") ?? "";
            var revoke = Named(ActionButton("撤销", () => _ = RevokeAppAsync(exe)), $"撤销 {exe} 的授权");
            list.Children.Add(SettingRow(exe, describe(entry), revoke));
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
        var exe = (_appsPick!.Text ?? "").Trim();
        if (exe.Length == 0 && _appsPick.SelectedItem is string chosen)
            exe = chosen;
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

    private async Task RevokeAppAsync(string exe)
    {
        if (await RequestAsync("apps_revoke", new Dictionary<string, object?> { ["exe"] = exe }) is { } result)
        {
            ApplyApps(result);
            ShowNotice("已撤销", $"AI 下次操作 {exe} 前会先问你。", InfoBarSeverity.Success);
        }
    }
}
