using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Windows.ApplicationModel.DataTransfer;

namespace Winhand.Desktop;

/// <summary>The "MCP 服务" page: local MCP servers that winhand offers to remote clients, each
/// at its own address (https://relay/mcp/&lt;name&gt;), configured here instead of in a file.</summary>
public sealed partial class MainWindow
{
    private JsonElement? _mcp;
    private StackPanel? _mcpRows;

    private UIElement BuildMcp()
    {
        _mcpRows = new StackPanel { Spacing = 12, Padding = new Thickness(24), MaxWidth = 1100 };
        RenderMcp();
        return new ScrollViewer { Content = _mcpRows };
    }

    private void ApplyMcp(JsonElement? state)
    {
        if (state is { ValueKind: JsonValueKind.Object })
            _mcp = state;
        RenderMcp();
    }

    private void RenderMcp()
    {
        if (_mcpRows is null)
            return;
        _mcpRows.Children.Clear();

        var intro = new Grid { ColumnSpacing = 16 };
        intro.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        intro.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        intro.Children.Add(Text(
            "这台电脑上的其他 MCP 服务（调试器、摄像头、CAD …）。每个启用的服务都有自己的远程地址，" +
            "在 claude.ai 里分别添加为自定义连接器即可使用；winhand 原样转发，不改名、不合并。第一次被调用时才启动。",
            "SecondaryCopyStyle"));
        var toolbar = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8, VerticalAlignment = VerticalAlignment.Top };
        toolbar.Children.Add(ActionButton("从其他客户端导入…", () => _ = ImportMcpAsync()));
        toolbar.Children.Add(ActionButton("添加服务", () => _ = EditMcpAsync(null), accent: true));
        Grid.SetColumn(toolbar, 1);
        intro.Children.Add(toolbar);
        _mcpRows.Children.Add(intro);

        if (Json.Str(_mcp, "base_url") is null)
            _mcpRows.Children.Add(Card(new StackPanel
            {
                Spacing = 8,
                Children =
                {
                    Text("还没有配置中转", "SectionHeadingStyle"),
                    Text("配置好中转后，这里的每个服务才有可以在 claude.ai 使用的远程地址。"),
                    ActionButton("去设置", () => ShowPage("settings"))
                }
            }));

        var servers = Json.Arr(_mcp, "servers").ToList();
        if (servers.Count == 0)
        {
            _mcpRows.Children.Add(Card(Text("还没有配置任何服务。点“添加服务”，或者从 Codex、Claude Desktop、Claude Code 的配置里导入。", "SecondaryCopyStyle")));
            return;
        }
        foreach (var server in servers)
            _mcpRows.Children.Add(McpCard(server));
    }

    private UIElement McpCard(JsonElement server)
    {
        var name = Json.Str(server, "name") ?? "";
        var enabled = Json.Bool(server, "enabled");
        var status = Json.Obj(server, "status");
        var state = Json.Str(status, "state") ?? "stopped";
        var http = Json.Str(server, "kind") == "http";
        var (label, tone) = !enabled ? ("已停用", Tone.Neutral) : state switch
        {
            "running" => (http ? "已就绪" : "运行中", Tone.Success),
            "starting" => ("启动中", Tone.Active),
            "error" => ("出错", Tone.Error),
            _ => ("未启动", Tone.Neutral)
        };

        var header = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 12 };
        header.Children.Add(Text(name, "SectionHeadingStyle"));
        header.Children.Add(Pill(label, tone));
        header.Children.Add(Pill(http ? "本机 HTTP" : "本地命令", Tone.Neutral));
        if (Json.Obj(status, "server") is { } info && Json.Str(info, "name") is { } serverName)
            header.Children.Add(Text($"{serverName} {Json.Str(info, "version")}", "SecondaryCopyStyle"));

        var body = new StackPanel { Spacing = 6 };
        body.Children.Add(header);
        if (Json.Str(server, "description") is { Length: > 0 } description)
            body.Children.Add(Text(description, "SecondaryCopyStyle"));
        var target = http
            ? Json.Str(server, "url") ?? ""
            : string.Join(" ", new[] { Json.Str(server, "command") ?? "" }.Concat(Json.Arr(server, "args").Select(a => Quote(a.GetString() ?? ""))));
        body.Children.Add(new TextBlock { Text = target, Style = Resource<Style>("DataCopyStyle"), TextWrapping = TextWrapping.Wrap, IsTextSelectionEnabled = true });

        if (Json.Str(server, "public_url") is { } publicUrl)
        {
            var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
            row.Children.Add(Text("远程地址", "SecondaryCopyStyle"));
            row.Children.Add(new TextBlock { Text = publicUrl, Style = Resource<Style>("DataCopyStyle"), IsTextSelectionEnabled = true, VerticalAlignment = VerticalAlignment.Center });
            row.Children.Add(Named(ActionButton("复制", () => CopyText(publicUrl, $"已复制 {name} 的地址", "在 claude.ai 的“设置 → 连接器 → 添加自定义连接器”里粘贴。")), $"复制 {name} 的远程地址"));
            body.Children.Add(row);
        }

        var facts = new List<string>();
        if (Json.Num(status, "calls") is > 0 and var calls)
            facts.Add($"工具调用 {calls:0} 次");
        if (Json.Num(status, "last_used") is { } lastUsed)
            facts.Add($"{Format.Since(lastUsed)}前使用");
        if (Json.Num(status, "pid") is { } pid)
            facts.Add($"进程 {pid:0}");
        if (Json.Num(status, "sessions") is > 0 and var sessions)
            facts.Add($"{sessions:0} 个远程会话");
        if (facts.Count > 0)
            body.Children.Add(Text(string.Join(" · ", facts), "SecondaryCopyStyle"));
        if (enabled && Json.Str(status, "error") is { Length: > 0 } error)
            body.Children.Add(Text(error, "ErrorCopyStyle"));

        var toggle = Named(new ToggleSwitch { IsOn = enabled, OnContent = "启用", OffContent = "停用", MinWidth = 0 }, $"启用 {name}");
        toggle.Toggled += async (_, _) =>
        {
            if (toggle.IsOn != enabled)
                ApplyMcp(await RequestAsync("mcp_set_enabled", new Dictionary<string, object?> { ["name"] = name, ["enabled"] = toggle.IsOn }));
        };
        var actions = new StackPanel { Spacing = 8, VerticalAlignment = VerticalAlignment.Top, HorizontalAlignment = HorizontalAlignment.Right };
        actions.Children.Add(toggle);
        var buttons = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        buttons.Children.Add(Named(ActionButton("测试", () => _ = TestMcpAsync(name)), $"测试 {name}"));
        if (!http && enabled)
        {
            var running = state is "running" or "starting";
            buttons.Children.Add(Named(ActionButton(running ? "停止" : "启动", () => _ = RequestMcpAsync(running ? "mcp_stop" : "mcp_start", name)), $"{(running ? "停止" : "启动")} {name}"));
        }
        buttons.Children.Add(Named(ActionButton("编辑", () => _ = EditMcpAsync(server)), $"编辑 {name}"));
        buttons.Children.Add(Named(ActionButton("删除", () => _ = DeleteMcpAsync(name)), $"删除 {name}"));
        actions.Children.Add(buttons);
        if (Json.Str(status, "log_file") is { } log)
        {
            var openLog = new HyperlinkButton { Content = "打开日志", HorizontalAlignment = HorizontalAlignment.Right };
            openLog.Click += (_, _) => OpenPath(log, select: true);
            actions.Children.Add(openLog);
        }

        var grid = new Grid { ColumnSpacing = 16 };
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        grid.Children.Add(body);
        Grid.SetColumn(actions, 1);
        grid.Children.Add(actions);
        return Card(grid);
    }

    private async Task RequestMcpAsync(string method, string name)
    {
        try
        {
            ApplyMcp(await _backend.CallAsync(method, new Dictionary<string, object?> { ["name"] = name }));
        }
        catch (Exception error)
        {
            ShowNotice($"{name} 没有启动", error.Message, InfoBarSeverity.Error);
        }
    }

    // ---------------------------------------------------------------- test

    private async Task TestMcpAsync(string name)
    {
        var progress = new ContentDialog
        {
            XamlRoot = Content.XamlRoot,
            Title = $"测试 {name}",
            Content = new StackPanel
            {
                Spacing = 12,
                Children =
                {
                    new ProgressRing { IsActive = true, HorizontalAlignment = HorizontalAlignment.Left },
                    Text("正在单独启动一份这个服务，列出它提供的工具……（不影响正在使用的那份）", "SecondaryCopyStyle")
                }
            },
            CloseButtonText = "关闭"
        };
        var shown = progress.ShowAsync();
        JsonElement? result = null;
        string? failure = null;
        try
        {
            result = await _backend.CallAsync("mcp_test", new Dictionary<string, object?> { ["name"] = name });
        }
        catch (Exception error)
        {
            failure = error.Message;
        }

        var report = new StackPanel { Spacing = 8 };
        if (result is { } r && Json.Bool(r, "ok"))
        {
            var info = Json.Obj(r, "server");
            report.Children.Add(Pill("可以使用", Tone.Success));
            report.Children.Add(Text($"{Json.Str(info, "name")} {Json.Str(info, "version")} · 协议 {Json.Str(r, "protocol")} · {Format.Duration(Json.Num(r, "duration_ms"))}", "SecondaryCopyStyle"));
            var tools = Json.Arr(r, "tools").ToList();
            report.Children.Add(Text($"{tools.Count} 个工具", "SectionHeadingStyle"));
            foreach (var tool in tools)
            {
                var line = Json.Str(tool, "name") ?? "";
                if (Json.Str(tool, "description") is { Length: > 0 } description)
                    line += $" — {description}";
                report.Children.Add(new TextBlock { Text = line, Style = Resource<Style>("DataCopyStyle"), TextWrapping = TextWrapping.Wrap });
            }
            var prompts = Json.Arr(r, "prompts").Count();
            if (prompts > 0 || Json.Num(r, "resources") is > 0)
                report.Children.Add(Text($"另有 {prompts} 个提示词、{Json.Num(r, "resources") ?? 0:0} 个资源", "SecondaryCopyStyle"));
        }
        else
        {
            report.Children.Add(Pill("启动失败", Tone.Error));
            report.Children.Add(new TextBlock { Text = failure ?? Json.Str(result, "error") ?? "未知错误", TextWrapping = TextWrapping.Wrap, IsTextSelectionEnabled = true });
            var tail = Json.Arr(result, "stderr_tail").Select(l => l.GetString()).ToList();
            if (tail.Count > 0)
            {
                report.Children.Add(Text("服务的最后输出", "SectionHeadingStyle"));
                report.Children.Add(new TextBlock { Text = string.Join("\n", tail), Style = Resource<Style>("DataCopyStyle"), TextWrapping = TextWrapping.Wrap, IsTextSelectionEnabled = true });
            }
        }
        progress.Content = new ScrollViewer { Content = report, MaxHeight = 460 };
        await shown;
    }

    // ---------------------------------------------------------------- add / edit

    private async Task EditMcpAsync(JsonElement? server)
    {
        var original = Json.Str(server, "name");
        var isHttp = Json.Str(server, "kind") == "http";
        var name = new TextBox { Header = "名称（会成为地址的一部分：…/mcp/名称）", Text = original ?? "", PlaceholderText = "pyocd-debug" };
        var kind = new RadioButtons { Header = "类型", MaxColumns = 2, Items = { "本地命令（stdio）", "本机 HTTP 地址" }, SelectedIndex = isHttp ? 1 : 0 };
        var command = new TextBox { Header = "启动命令", Text = Json.Str(server, "command") ?? "", PlaceholderText = "uv、npx、python 或 exe 的完整路径" };
        var args = new TextBox
        {
            Header = "参数（每行一个）",
            Text = string.Join("\r", Json.Arr(server, "args").Select(a => a.GetString())),
            AcceptsReturn = true,
            TextWrapping = TextWrapping.Wrap,
            MinHeight = 80,
            PlaceholderText = "--directory\rD:\\Dev\\my-mcp\rrun\rmy-mcp"
        };
        var cwd = new TextBox { Header = "工作目录（可选）", Text = Json.Str(server, "cwd") ?? "" };
        var env = new TextBox
        {
            Header = "环境变量（每行 名称=值；密钥显示为 •••，不改就原样保留）",
            Text = PairsText(Json.Obj(server, "env"), "="),
            AcceptsReturn = true,
            TextWrapping = TextWrapping.Wrap,
            MinHeight = 60
        };
        var url = new TextBox { Header = "地址", Text = Json.Str(server, "url") ?? "", PlaceholderText = "http://127.0.0.1:8000/mcp" };
        var headers = new TextBox
        {
            Header = "请求头（可选，每行 名称: 值）",
            Text = PairsText(Json.Obj(server, "headers"), ": "),
            AcceptsReturn = true,
            TextWrapping = TextWrapping.Wrap,
            MinHeight = 60
        };
        var description = new TextBox { Header = "说明（可选）", Text = Json.Str(server, "description") ?? "" };
        var timeout = new NumberBox { Header = "启动超时（秒）", Value = Json.Num(server, "startup_timeout_s") ?? 60, Minimum = 5, Maximum = 600, SpinButtonPlacementMode = NumberBoxSpinButtonPlacementMode.Compact };
        var idle = new NumberBox { Header = "闲置多少分钟后停止（0 = 一直运行）", Value = Json.Num(server, "idle_stop_minutes") ?? 0, Minimum = 0, Maximum = 1440, SpinButtonPlacementMode = NumberBoxSpinButtonPlacementMode.Compact };
        var enabled = new CheckBox { Content = "启用", IsChecked = server is null || Json.Bool(server, "enabled") };
        var error = Text("", "ErrorCopyStyle");
        error.Visibility = Visibility.Collapsed;

        var stdioFields = new StackPanel { Spacing = 12, Children = { command, args, cwd, env, timeout, idle } };
        var httpFields = new StackPanel { Spacing = 12, Children = { url, headers } };
        void SyncKind()
        {
            stdioFields.Visibility = kind.SelectedIndex == 0 ? Visibility.Visible : Visibility.Collapsed;
            httpFields.Visibility = kind.SelectedIndex == 1 ? Visibility.Visible : Visibility.Collapsed;
        }
        kind.SelectionChanged += (_, _) => SyncKind();
        SyncKind();

        var form = new StackPanel { Spacing = 12, MinWidth = 520, Children = { name, kind, stdioFields, httpFields, description, enabled, error } };
        var dialog = new ContentDialog
        {
            XamlRoot = Content.XamlRoot,
            Title = original is null ? "添加 MCP 服务" : $"编辑 {original}",
            Content = new ScrollViewer { Content = form, MaxHeight = 560, Padding = new Thickness(0, 0, 16, 0) },
            PrimaryButtonText = "保存",
            CloseButtonText = "取消",
            DefaultButton = ContentDialogButton.Primary
        };
        dialog.PrimaryButtonClick += async (_, e) =>
        {
            var deferral = e.GetDeferral();
            try
            {
                var entry = new Dictionary<string, object?>
                {
                    ["name"] = name.Text.Trim(),
                    ["description"] = description.Text.Trim(),
                    ["enabled"] = enabled.IsChecked == true
                };
                if (kind.SelectedIndex == 1)
                {
                    entry["url"] = url.Text.Trim();
                    entry["headers"] = ParsePairs(headers.Text, ':');
                }
                else
                {
                    entry["command"] = command.Text.Trim();
                    entry["args"] = Lines(args.Text);
                    entry["cwd"] = cwd.Text.Trim();
                    entry["env"] = ParsePairs(env.Text, '=');
                    entry["startup_timeout_s"] = double.IsNaN(timeout.Value) ? 60 : timeout.Value;
                    entry["idle_stop_minutes"] = double.IsNaN(idle.Value) ? 0 : idle.Value;
                }
                var parameters = new Dictionary<string, object?> { ["entry"] = entry };
                if (original is not null)
                    parameters["original_name"] = original;
                ApplyMcp(await _backend.CallAsync("mcp_save", parameters));
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
    }

    private async Task DeleteMcpAsync(string name)
    {
        var dialog = new ContentDialog
        {
            XamlRoot = Content.XamlRoot,
            Title = $"删除 {name}？",
            Content = "会停止这个服务并从 winhand 的配置里移除。claude.ai 里对应的连接器之后会提示找不到服务，可以一并删掉。服务本身的程序和数据不受影响。",
            PrimaryButtonText = "删除",
            CloseButtonText = "取消",
            DefaultButton = ContentDialogButton.Close
        };
        if (await dialog.ShowAsync() == ContentDialogResult.Primary)
            ApplyMcp(await RequestAsync("mcp_delete", new Dictionary<string, object?> { ["name"] = name }));
    }

    // ---------------------------------------------------------------- import

    private async Task ImportMcpAsync()
    {
        if (await RequestAsync("mcp_list") is not { } listed)
            return;
        ApplyMcp(listed);
        var candidates = Json.Arr(listed, "candidates").ToList();
        var panel = new StackPanel { Spacing = 8, MinWidth = 520 };
        var boxes = new List<(CheckBox Box, string Name)>();
        if (candidates.Count == 0)
            panel.Children.Add(Text("没有在 Codex、Claude Desktop、Claude Code 的配置里找到 MCP 服务。", "SecondaryCopyStyle"));
        else
            panel.Children.Add(Text("只复制配置到 winhand；原来的客户端照常使用，之后两边互不影响。", "SecondaryCopyStyle"));
        foreach (var candidate in candidates)
        {
            var name = Json.Str(candidate, "name") ?? "";
            var already = Json.Bool(candidate, "already");
            var box = Named(new CheckBox
            {
                IsChecked = !already,
                IsEnabled = !already,
                Content = new StackPanel
                {
                    Children =
                    {
                        Text(already ? $"{name}（已在 winhand 里）" : name),
                        new TextBlock { Text = $"{Json.Str(candidate, "source")} · {Json.Str(candidate, "summary")}", Style = Resource<Style>("DataCopyStyle"), TextWrapping = TextWrapping.Wrap }
                    }
                }
            }, name);
            boxes.Add((box, name));
            panel.Children.Add(box);
        }
        var dialog = new ContentDialog
        {
            XamlRoot = Content.XamlRoot,
            Title = "从其他客户端导入",
            Content = new ScrollViewer { Content = panel, MaxHeight = 480 },
            PrimaryButtonText = candidates.Count > 0 ? "导入选中的" : "",
            CloseButtonText = "取消",
            DefaultButton = candidates.Count > 0 ? ContentDialogButton.Primary : ContentDialogButton.Close
        };
        if (await dialog.ShowAsync() != ContentDialogResult.Primary)
            return;
        var names = boxes.Where(b => b.Box.IsChecked == true && b.Box.IsEnabled).Select(b => b.Name).ToList();
        if (names.Count == 0)
            return;
        if (await RequestAsync("mcp_import", new Dictionary<string, object?> { ["names"] = names }) is { } result)
        {
            ApplyMcp(result);
            var added = Json.Arr(result, "added").Select(a => a.GetString()).ToList();
            ShowNotice($"导入了 {added.Count} 个服务", added.Count > 0 ? "可以先点“测试”确认能启动，再把远程地址添加到 claude.ai。" : "选中的服务已经存在。", InfoBarSeverity.Success);
        }
    }

    // ---------------------------------------------------------------- helpers

    private void CopyText(string text, string title, string message)
    {
        var package = new DataPackage();
        package.SetText(text);
        Clipboard.SetContent(package);
        ShowNotice(title, message, InfoBarSeverity.Success);
    }

    /// <summary>What screen readers (and UI automation) call a control whose visible text repeats on every card.</summary>
    private static T Named<T>(T element, string name) where T : DependencyObject
    {
        Microsoft.UI.Xaml.Automation.AutomationProperties.SetName(element, name);
        return element;
    }

    private static string Quote(string arg) => arg.Contains(' ') ? $"\"{arg}\"" : arg;

    private static List<string> Lines(string text) =>
        text.Split('\r', '\n').Select(l => l.Trim()).Where(l => l.Length > 0).ToList();

    private static string PairsText(JsonElement? pairs, string separator) =>
        pairs is { ValueKind: JsonValueKind.Object } p
            ? string.Join("\r", p.EnumerateObject().Select(kv => $"{kv.Name}{separator}{kv.Value.GetString()}"))
            : "";

    private static Dictionary<string, string> ParsePairs(string text, char separator)
    {
        var pairs = new Dictionary<string, string>();
        foreach (var line in Lines(text))
        {
            var at = line.IndexOf(separator);
            if (at <= 0)
                throw new FormatException($"这一行缺少“{separator}”：{line}");
            pairs[line[..at].Trim()] = line[(at + 1)..].Trim();
        }
        return pairs;
    }
}
