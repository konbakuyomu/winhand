using System.Runtime.InteropServices;
using System.Text.Json;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
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
    private TextBlock? _secretsError;
    private string? _shownSecret, _shownValue;
    private DispatcherQueueTimer? _secretTimer;

    private UIElement BuildSecretsSection()
    {
        _secretsList = new StackPanel { Spacing = 12 };
        _secretsError = Text("", "ErrorCopyStyle");
        _secretsError.TextWrapping = TextWrapping.Wrap;
        _secretsError.Visibility = Visibility.Collapsed;
        var add = ActionButton("添加密钥", () => _ = EditSecretAsync(null));
        return Section("密钥",
            "AI 让程序带上这些名字运行时，程序拿到对应的值，AI 自己看不到。只保存在这台电脑的 secrets.env 里，只有你的 Windows 账户能读。" +
            "显示的值 30 秒后自动隐藏，显示期间截图拍不到这个窗口。",
            _secretsError,
            Card(new StackPanel { Spacing = 12, Children = { _secretsList, Divider(), ActionRow(add) } }));
    }

    private void ApplySecrets(JsonElement? secrets)
    {
        if (secrets is not { ValueKind: JsonValueKind.Object })
            return;
        _secrets = secrets;
        if (_shownSecret is { } shown && !Json.Arr(secrets, "items").Any(item => Json.Str(item, "name") == shown))
            HideSecret(); // deleted or renamed meanwhile
        RenderSecrets();
    }

    private void RenderSecrets()
    {
        if (_secretsList is null || _secrets is not { } secrets)
            return;
        var error = Json.Str(secrets, "error");
        _secretsError!.Text = error is null ? "" : $"读不了密钥文件：{error}";
        _secretsError.Visibility = error is null ? Visibility.Collapsed : Visibility.Visible;

        _secretsList.Children.Clear();
        var items = Json.Arr(secrets, "items").ToList();
        if (items.Count == 0)
        {
            _secretsList.Children.Add(Text("还没有密钥。AI 需要时会弹框请你输入，也可以在这里添加。", "SecondaryCopyStyle"));
            return;
        }
        foreach (var item in items)
        {
            if (_secretsList.Children.Count > 0)
                _secretsList.Children.Add(Divider());
            var name = Json.Str(item, "name") ?? "";
            var shown = name == _shownSecret;
            var chars = Json.Num(item, "chars") ?? 0;
            var verb = shown ? "隐藏" : "显示";
            TextBlock detail;
            if (shown)
            {
                detail = Text(_shownValue ?? "", "DataCopyStyle");
                detail.IsTextSelectionEnabled = true;
                detail.TextWrapping = TextWrapping.Wrap;
            }
            else
                detail = Text($"{chars:0} 个字符", "SecondaryCopyStyle");
            var actions = ActionRow(
                Named(ActionButton(verb, () => _ = ToggleSecretAsync(name)), $"{verb} {name} 的值"),
                Named(ActionButton("复制", () => _ = CopySecretAsync(name)), $"复制 {name} 的值"),
                Named(ActionButton("修改", () => _ = EditSecretAsync(name)), $"修改 {name}"),
                Named(ActionButton("删除", () => _ = DeleteSecretAsync(name)), $"删除 {name}"));
            _secretsList.Children.Add(SettingRow(name, detail, actions));
        }
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
        RenderSecrets();
    }

    /// <summary>Hides a shown value: its button, after 30 s, or on leaving the page or the window.</summary>
    private void HideSecret()
    {
        if (_shownSecret is null)
            return;
        _shownSecret = _shownValue = null;
        _secretTimer?.Stop();
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

    private async Task CopySecretAsync(string name)
    {
        var parameters = new Dictionary<string, object?> { ["name"] = name, ["copy"] = true };
        if (await RequestAsync("secrets_reveal", parameters) is not { } result)
            return;
        var package = new DataPackage();
        package.SetText(Json.Str(result, "value") ?? "");
        // kept out of the clipboard history (Win+V) and not synced to the person's other devices
        Clipboard.SetContentWithOptions(package, new ClipboardContentOptions { IsAllowedInHistory = false, IsRoamable = false });
        ShowNotice("已复制", $"{name} 的值已复制到剪贴板，不会进剪贴板历史，也不会同步到其他设备。", InfoBarSeverity.Success);
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
