using System.Runtime.InteropServices;
using System.Text.Json;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using Windows.ApplicationModel.DataTransfer;

namespace Winhand.Desktop;

/// <summary>The "密钥" section of the settings page: this machine's secret store (secrets.env), whose
/// values the AI hands to programs with run/job_start(secrets=[...]) without seeing them. The person
/// adds, changes, renames, deletes, shows and copies them here (PROTOCOL.md: secrets_list,
/// secrets_save, secrets_delete, secrets_reveal, secrets_hide and the secrets event). A shown value
/// hides itself after 30 s; while it is shown the window is left out of screen captures and
/// winhand takes no screenshots, since a picture would carry it past the masking of results.</summary>
public sealed partial class MainWindow
{
    private const uint WdaNone = 0, WdaExcludeFromCapture = 0x11;
    private static readonly TimeSpan SecretShownFor = TimeSpan.FromSeconds(30);

    private JsonElement? _secrets;
    private StackPanel? _secretsList;
    private InfoBar? _secretsError, _secretProtection;
    private Grid? _secretBanner;
    private ProgressBar? _secretProgress;
    private AutoSuggestBox? _secretsSearch;
    private ContentControl? _secretsCount, _shownCountdown;
    private string? _shownSecret, _shownValue;
    private DispatcherQueueTimer? _secretTimer, _secretCountdownTimer;
    private DateTimeOffset _secretShownUntil;
    private bool _previewSecretShown;

    private UIElement BuildSecretsSection()
    {
        var page = PagePanel();
        page.Spacing = 16;
        _secretsCount = new ContentControl { Content = "0", Style = Resource<Style>("StatusBadgeNeutralStyle") };
        var heading = ActionRow(Text("密钥", "PageTitleStyle"), _secretsCount, PageHelpButton("密钥",
            "AI 让程序带上这些名字运行时，程序拿到对应的值，AI 自己看不到。只保存在这台电脑的 secrets.env 里，只有你的 Windows 账户能读。" +
            "显示的值 30 秒后自动隐藏，显示期间截图拍不到这个窗口。"));
        page.Children.Add(PageControlRow(heading, PageGlyphButton("添加密钥", "\uE710", () => _ = EditSecretAsync(null), "AccentButtonStyle")));

        _secretProtection = new InfoBar
        {
            Severity = InfoBarSeverity.Warning, IsClosable = false, IsOpen = true,
            Title = "防截屏保护已开启", ActionButton = ActionButton("立即隐藏", HideSecret)
        };
        _secretProgress = new ProgressBar
        {
            Minimum = 0, Maximum = 30, Height = 2, MinHeight = 2, VerticalAlignment = VerticalAlignment.Bottom,
            Foreground = Resource<Brush>("SystemFillColorCautionBrush"), IsHitTestVisible = false
        };
        _secretBanner = new Grid { Visibility = Visibility.Collapsed, Children = { _secretProtection, _secretProgress } };
        page.Children.Add(_secretBanner);
        _secretsError = new InfoBar
        {
            Severity = InfoBarSeverity.Error, IsClosable = false, IsOpen = true, Visibility = Visibility.Collapsed,
            Title = "读不了密钥文件",
            ActionButton = PageGlyphButton("打开文件位置", "\uE8DA", () => OpenPath(Json.Str(_secrets, "file"), select: true))
        };
        page.Children.Add(_secretsError);
        _secretsSearch = Named(new AutoSuggestBox
        {
            PlaceholderText = "搜索密钥名称… (Ctrl+F)", QueryIcon = new FontIcon { Glyph = "\uE721", FontFamily = new FontFamily("Segoe Fluent Icons, Segoe MDL2 Assets") },
            Width = 320, HorizontalAlignment = HorizontalAlignment.Left
        }, "搜索密钥名称");
        _secretsSearch.TextChanged += (_, _) => RenderSecrets();
        page.Children.Add(PageControlRow(_secretsSearch, PageGlyphButton("secrets.env", "\uE8DA", () => OpenPath(Json.Str(_secrets, "file"), select: true))));
        var search = new KeyboardAccelerator { Key = Windows.System.VirtualKey.F, Modifiers = Windows.System.VirtualKeyModifiers.Control };
        search.Invoked += (_, args) => { _secretsSearch.Focus(FocusState.Keyboard); args.Handled = true; };
        page.KeyboardAccelerators.Add(search);
        _secretsList = new StackPanel();
        var card = Card(_secretsList);
        card.Padding = new Thickness(0);
        page.Children.Add(card);
        return PageScroll(page);
    }

    private void ApplySecrets(JsonElement? secrets)
    {
        if (secrets is not { ValueKind: JsonValueKind.Object })
            return;
        _secrets = secrets;
        if (_shownSecret is { } shown && !Json.Arr(secrets, "items").Any(item => Json.Str(item, "name") == shown))
            HideSecret(); // deleted or renamed meanwhile
        RenderSecrets();
        if (!_previewSecretShown && PreviewState("revealed") && Json.Arr(secrets, "items").FirstOrDefault() is { ValueKind: JsonValueKind.Object } first)
        {
            _previewSecretShown = true;
            _ = ToggleSecretAsync(Json.Str(first, "name")!);
        }
    }

    private void RenderSecrets()
    {
        if (_secretsList is null || _secrets is not { } secrets)
            return;
        var error = Json.Str(secrets, "error");
        _secretsError!.Message = error ?? "";
        _secretsError.Visibility = string.IsNullOrEmpty(error) ? Visibility.Collapsed : Visibility.Visible;
        _secretBanner!.Visibility = _shownSecret is null ? Visibility.Collapsed : Visibility.Visible;

        _secretsList.Children.Clear();
        _shownCountdown = null;
        var items = Json.Arr(secrets, "items").ToList();
        _secretsCount!.Content = items.Count.ToString();
        var matching = items.Where(item => (Json.Str(item, "name") ?? "").Contains(_secretsSearch!.Text, StringComparison.OrdinalIgnoreCase)).ToList();
        if (matching.Count == 0)
        {
            var empty = items.Count == 0
                ? EmptyState("还没有密钥", "AI 需要时会弹框请你输入，也可以在这里添加。", PageGlyphButton("添加密钥", "\uE710", () => _ = EditSecretAsync(null), "AccentButtonStyle"))
                : EmptyState("没有匹配的密钥", "试试其他名称，或清除搜索。", PageGlyphButton("清除搜索", "\uE721", () => _secretsSearch!.Text = ""));
            empty.HorizontalAlignment = HorizontalAlignment.Center;
            empty.Margin = new Thickness(20);
            _secretsList.Children.Add(empty);
            return;
        }
        foreach (var item in matching)
        {
            if (_secretsList.Children.Count > 0)
            {
                var divider = Divider();
                divider.Margin = new Thickness(16, 0, 16, 0);
                _secretsList.Children.Add(divider);
            }
            var name = Json.Str(item, "name") ?? "";
            var shown = name == _shownSecret;
            var chars = Json.Num(item, "chars") ?? 0;
            var verb = shown ? "隐藏" : "显示";
            var title = Text(name, "MonoTitleStyle");
            title.TextWrapping = TextWrapping.NoWrap;
            title.TextTrimming = TextTrimming.CharacterEllipsis;
            var details = new StackPanel { Spacing = 4, Children = { title } };
            if (shown)
            {
                var value = Text(_shownValue ?? "", "MonoDataStyle");
                value.IsTextSelectionEnabled = true;
                _shownCountdown = new ContentControl { Style = Resource<Style>("StatusBadgeWarningStyle") };
                details.Children.Add(new Border
                {
                    BorderBrush = Resource<Brush>("WinhandWarningTintBorderBrush"), BorderThickness = new Thickness(1),
                    CornerRadius = new CornerRadius(4), Background = Resource<Brush>("WinhandCodeBgBrush"), Padding = new Thickness(8, 4, 8, 4),
                    Child = PageControlRow(value, _shownCountdown)
                });
            }
            else
                details.Children.Add(Text($"•••••••••••• · {chars:0} 个字符", "CaptionTextStyle"));
            Button? copy = null;
            copy = Named(PageGlyphButton("复制", "\uE8C8", () => _ = CopySecretAsync(name, copy!)), $"复制 {name} 的值");
            ToolTipService.SetToolTip(copy, "复制到剪贴板，不进剪贴板历史，也不同步到其他设备");
            var edit = Named(new MenuFlyoutItem { Text = "修改名称或值…" }, $"修改 {name}");
            edit.Click += (_, _) => _ = EditSecretAsync(name);
            var delete = Named(new MenuFlyoutItem { Text = "删除密钥…", Foreground = Resource<Brush>("SystemFillColorCriticalBrush") }, $"删除 {name}");
            delete.Click += (_, _) => _ = DeleteSecretAsync(name);
            var more = Named(new Button
            {
                Style = Resource<Style>("IconGhostButtonStyle"),
                Content = new FontIcon { Glyph = "\uE712", FontFamily = new FontFamily("Segoe Fluent Icons, Segoe MDL2 Assets"), FontSize = 14 },
                Flyout = new MenuFlyout { Items = { edit, new MenuFlyoutSeparator(), delete } }
            }, $"{name} 的更多操作");
            var actions = ActionRow(
                Named(PageGlyphButton(verb, shown ? "\uED1A" : "\uE7B3", () => _ = ToggleSecretAsync(name), shown ? "WarningTintedButtonStyle" : "GhostButtonStyle"), $"{verb} {name} 的值"),
                copy, more);
            var row = PageControlRow(details, actions);
            row.Padding = new Thickness(16, 12, 16, 12);
            _secretsList.Children.Add(row);
        }
        UpdateSecretCountdown();
    }

    private void UpdateSecretCountdown()
    {
        if (_shownSecret is null)
            return;
        var seconds = Math.Max(0, (int)Math.Ceiling((_secretShownUntil - DateTimeOffset.UtcNow).TotalSeconds));
        _secretProtection!.Message = $"正在显示 {_shownSecret}，{seconds} 秒后自动隐藏，期间无法捕获此窗口";
        _secretProgress!.Value = seconds;
        if (_shownCountdown is not null)
            _shownCountdown.Content = $"{seconds}s";
    }

    private async Task RefreshSecretsAsync()
    {
        if (!_backend.IsRunning)
            return;
        try
        {
            ApplySecrets(await _backend.CallAsync("secrets_list"));
        }
        catch (Exception)
        {
            // the section keeps what it showed; the next secrets event or visit brings it up to date
        }
    }

    private async Task ToggleSecretAsync(string name)
    {
        if (_shownSecret == name)
        {
            HideSecret();
            return;
        }
        SetWindowDisplayAffinity(_hwnd, WdaExcludeFromCapture); // before the value is drawn
        if (await RequestAsync("secrets_reveal", new Dictionary<string, object?> { ["name"] = name }) is not { } result)
        {
            if (_shownSecret is null)
                SetWindowDisplayAffinity(_hwnd, WdaNone);
            return;
        }
        _shownSecret = name;
        _shownValue = Json.Str(result, "value") ?? "";
        if (_secretTimer is null)
        {
            _secretTimer = DispatcherQueue.CreateTimer();
            _secretTimer.Interval = SecretShownFor;
            _secretTimer.IsRepeating = false;
            _secretTimer.Tick += (_, _) => HideSecret();
        }
        _secretTimer.Stop();
        _secretTimer.Start();
        _secretShownUntil = DateTimeOffset.UtcNow + SecretShownFor;
        if (_secretCountdownTimer is null)
        {
            _secretCountdownTimer = DispatcherQueue.CreateTimer();
            _secretCountdownTimer.Interval = TimeSpan.FromSeconds(1);
            _secretCountdownTimer.Tick += (_, _) => UpdateSecretCountdown();
        }
        _secretCountdownTimer.Stop();
        _secretCountdownTimer.Start();
        RenderSecrets();
    }

    /// <summary>Hides a shown value: its button, after 30 s, or on leaving the page or the window.</summary>
    private void HideSecret()
    {
        if (_shownSecret is null)
            return;
        _shownSecret = _shownValue = null;
        _secretTimer?.Stop();
        _secretCountdownTimer?.Stop();
        RenderSecrets();
        SetWindowDisplayAffinity(_hwnd, WdaNone);
        _ = SecretHiddenAsync();
    }

    private async Task SecretHiddenAsync()
    {
        if (!_backend.IsRunning)
            return;
        try
        {
            await _backend.CallAsync("secrets_hide"); // winhand's screenshots may go on
        }
        catch (Exception)
        {
            // the backend lets them go on by itself after 35 s
        }
    }

    private async Task CopySecretAsync(string name, Button button)
    {
        var parameters = new Dictionary<string, object?> { ["name"] = name, ["copy"] = true };
        if (await RequestAsync("secrets_reveal", parameters) is not { } result)
            return;
        var package = new DataPackage();
        package.SetText(Json.Str(result, "value") ?? "");
        // kept out of the clipboard history (Win+V) and not synced to the person's other devices
        Clipboard.SetContentWithOptions(package, new ClipboardContentOptions { IsAllowedInHistory = false, IsRoamable = false });
        var label = (TextBlock)((StackPanel)button.Content).Children[1];
        label.Text = "已复制";
        button.Foreground = Resource<Brush>("SystemFillColorSuccessBrush");
        var timer = DispatcherQueue.CreateTimer();
        timer.Interval = TimeSpan.FromSeconds(1.5);
        timer.IsRepeating = false;
        timer.Tick += (_, _) => { label.Text = "复制"; button.ClearValue(Button.ForegroundProperty); };
        timer.Start();
    }

    private async Task EditSecretAsync(string? original)
    {
        var name = new TextBox { Header = "名字", Text = original ?? "", PlaceholderText = "例如 CLOUDFLARE_API_TOKEN" };
        var value = new PasswordBox { Header = "值", PlaceholderText = original is null ? "粘贴密钥的值" : "留空保持不变" };
        var hint = Text("名字用英文字母、数字和下划线，AI 用这个名字请程序带上它。改名后，用旧名字的地方要跟着改。", "SecondaryCopyStyle");
        hint.TextWrapping = TextWrapping.Wrap;
        var error = Text("", "ErrorCopyStyle");
        error.TextWrapping = TextWrapping.Wrap;
        error.Visibility = Visibility.Collapsed;
        var dialog = new ContentDialog
        {
            XamlRoot = Content.XamlRoot,
            Title = original is null ? "添加密钥" : $"修改 {original}",
            Content = new StackPanel { Spacing = 16, Width = 440, Children = { name, value, hint, error } },
            PrimaryButtonText = "保存",
            CloseButtonText = "取消",
            DefaultButton = ContentDialogButton.Primary
        };
        dialog.PrimaryButtonClick += async (_, e) =>
        {
            var deferral = e.GetDeferral();
            try
            {
                var parameters = new Dictionary<string, object?> { ["name"] = name.Text.Trim(), ["value"] = value.Password };
                if (original is not null)
                    parameters["original_name"] = original;
                var saved = await _backend.CallAsync("secrets_save", parameters);
                if (original is not null && _shownSecret == original)
                    HideSecret(); // what it showed is no longer the value
                ApplySecrets(saved);
            }
            catch (Exception failure)
            {
                error.Text = failure.Message;
                error.Visibility = Visibility.Visible;
                e.Cancel = true;
            }
            finally
            {
                deferral.Complete();
            }
        };
        await dialog.ShowAsync();
        value.Password = "";
    }

    private async Task DeleteSecretAsync(string name)
    {
        var dialog = new ContentDialog
        {
            XamlRoot = Content.XamlRoot,
            Title = $"删除 {name}？",
            Content = Text("用到它的程序之后拿不到它；以后要用，得重新输入它的值。"),
            PrimaryButtonText = "删除",
            CloseButtonText = "取消",
            DefaultButton = ContentDialogButton.Close
        };
        if (await dialog.ShowAsync() != ContentDialogResult.Primary)
            return;
        if (_shownSecret == name)
            HideSecret();
        ApplySecrets(await RequestAsync("secrets_delete", new Dictionary<string, object?> { ["name"] = name }));
    }

    [DllImport("user32.dll")] private static extern bool SetWindowDisplayAffinity(nint hWnd, uint affinity);
}
