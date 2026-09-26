using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Windows.ApplicationModel.DataTransfer;

namespace Winhand.Desktop;

/// <summary>The "MCP 服务" page: local MCP servers that winhand offers to remote clients, each at
/// its own address (https://relay/mcp/&lt;name&gt;). A list of services on the left, the selected
/// one on the right: whether it is offered, its address, how it runs, how it is started.</summary>
public sealed partial class MainWindow
{
    private JsonElement? _mcp;
    private string? _mcpSelection;
    private ListView? _mcpList;
    private ContentControl? _mcpDetail;
    private bool _renderingMcpList;
    private readonly HashSet<string> _mcpBusy = [];

    private UIElement BuildMcp()
    {
        var add = ActionButton("添加服务", () => _ = EditMcpAsync(null), accent: true);
        var import = ActionButton("导入…", () => _ = ImportMcpAsync());
        ToolTipService.SetToolTip(import, "从 Codex、Claude Desktop、Claude Code 的配置导入");
        Named(import, "从其他客户端导入");
        _mcpList = new ListView { SelectionMode = ListViewSelectionMode.Single };
        Named(_mcpList, "MCP 服务");
        _mcpList.SelectionChanged += (_, _) =>
        {
            if (_renderingMcpList || _mcpList.SelectedItem is not ListViewItem { Tag: string name })
                return;
            _mcpSelection = name;
            RenderMcpDetail();
        };
        _mcpDetail = new ContentControl { HorizontalContentAlignment = HorizontalAlignment.Stretch, VerticalContentAlignment = VerticalAlignment.Stretch };
        RenderMcp();
        return SplitWorkspace("mcp", ListPane(ActionRow(add, import), _mcpList), _mcpDetail, 260, 220, 360);
    }

    private void ApplyMcp(JsonElement? state)
    {
        if (state is { ValueKind: JsonValueKind.Object })
            _mcp = state;
        RenderMcp();
    }

    private List<JsonElement> McpServers => Json.Arr(_mcp, "servers").ToList();

    private void RenderMcp()
    {
        RenderMcpList();
        RenderMcpDetail();
    }

    /// <summary>What a service's state means to a person, in one label and one line.</summary>
    private static (string Label, string Detail, Tone Tone) McpState(JsonElement server)
    {
        var status = Json.Obj(server, "status");
        var info = Json.Obj(status, "server");
        var identity = Json.Str(info, "name") is { } serverName ? $"{serverName} {Json.Str(info, "version")}".Trim() : "";
        if (!Json.Bool(server, "enabled"))
            return ("未对外提供", "远程客户端无法调用", Tone.Neutral);
        if (Json.Str(server, "kind") == "http")
            return Json.Str(status, "error") is { Length: > 0 } httpError
                ? ("无法连接", FirstLine(httpError), Tone.Error)
                : ("可用", "转发到本机 HTTP 服务", Tone.Success);
        return Json.Str(status, "state") switch
        {
            "running" => ("运行中", identity.Length > 0 ? identity : "已启动", Tone.Success),
            "starting" => ("启动中", "正在启动并握手", Tone.Active),
            "error" => ("出错", FirstLine(Json.Str(status, "error") ?? "启动失败"), Tone.Error),
            _ => ("待命", "第一次被调用时启动", Tone.Neutral)
        };
    }

    private static string FirstLine(string text)
    {
        var line = text.Split('\n')[0].Trim();
        return line.Length > 120 ? line[..120] + "…" : line;
    }

    private void RenderMcpList()
    {
        if (_mcpList is null)
            return;
        _renderingMcpList = true;
        _mcpList.Items.Clear();
        var servers = McpServers;
        if (_mcpSelection is null || servers.All(s => Json.Str(s, "name") != _mcpSelection))
            _mcpSelection = servers.Select(s => Json.Str(s, "name")).FirstOrDefault();
        foreach (var server in servers)
        {
            var name = Json.Str(server, "name") ?? "";
            var (label, detail, tone) = McpState(server);
            var item = ListRow(name, name, $"{label} · {detail}", tone);
            _mcpList.Items.Add(item);
            if (name == _mcpSelection)
                _mcpList.SelectedItem = item;
        }
        _renderingMcpList = false;
    }

    private void RenderMcpDetail()
    {
        if (_mcpDetail is null)
            return;
        var servers = McpServers;
        if (servers.Count == 0)
        {
            var page = PagePanel();
            page.Children.Add(EmptyState(
                "还没有 MCP 服务",
                "把这台电脑上的其他 MCP 服务（调试器、摄像头、CAD …）交给 winhand，它们就各自有一个远程地址，" +
                "在 claude.ai 里添加为自定义连接器即可使用。winhand 原样转发，不改工具名，也不和 winhand 自己的工具混在一起。",
                ActionButton("从其他客户端导入…", () => _ = ImportMcpAsync(), accent: true),
                ActionButton("手动添加", () => _ = EditMcpAsync(null))));
            _mcpDetail.Content = DetailPane(page);
            return;
        }
        var selected = servers.FirstOrDefault(s => Json.Str(s, "name") == _mcpSelection);
        if (selected.ValueKind != JsonValueKind.Object)
            selected = servers[0];
        _mcpDetail.Content = DetailPane(McpDetail(selected));
    }

    private StackPanel McpDetail(JsonElement server)
    {
        var name = Json.Str(server, "name") ?? "";
        var enabled = Json.Bool(server, "enabled");
        var http = Json.Str(server, "kind") == "http";
        var status = Json.Obj(server, "status");
        var (label, detail, tone) = McpState(server);
        var busy = _mcpBusy.Contains(name);
        var page = PagePanel();

        page.Children.Add(DetailHeader(name, Json.Str(server, "description") ?? "", Pill(label, tone), null));

        // ---- remote access
        var offered = CompactSwitch($"对外提供 {name}", enabled);
        offered.Toggled += async (_, _) =>
        {
            offered.IsEnabled = false;
            ApplyMcp(await RequestAsync("mcp_set_enabled", new Dictionary<string, object?> { ["name"] = name, ["enabled"] = offered.IsOn }));
        };
        var publicUrl = Json.Str(server, "public_url");
        var address = new StackPanel { Spacing = 4, VerticalAlignment = VerticalAlignment.Center };
        address.Children.Add(Text("远程地址", "LabelCopyStyle"));
        if (publicUrl is not null)
        {
            var url = Text(publicUrl, "DataCopyStyle");
            url.FontSize = 13;
            address.Children.Add(url);
        }
        else
            address.Children.Add(Text("在设置里配置中转后生成。", "SecondaryCopyStyle"));
        FrameworkElement addressAction = publicUrl is not null
            ? Named(ActionButton("复制", () => CopyText(publicUrl, $"已复制 {name} 的地址",
                "在 claude.ai 的“设置 → 连接器 → 添加自定义连接器”里粘贴；第一次连接时输入 winhand 口令授权。")), $"复制 {name} 的远程地址")
            : ActionButton("去设置", () => ShowPage("settings"));
        var addressRow = new Grid { ColumnSpacing = 20 };
        addressRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        addressRow.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        addressRow.Children.Add(address);
        addressAction.VerticalAlignment = VerticalAlignment.Center;
        Grid.SetColumn(addressAction, 1);
        addressRow.Children.Add(addressAction);
        page.Children.Add(Section("远程访问", "",
            RowsPanel(
                SettingRow("对外提供", enabled
                    ? "claude.ai 等远程客户端可以通过下面的地址调用它。"
                    : "已关闭：远程客户端无法调用，正在运行的进程已停止。", offered),
                addressRow)));

        // ---- runtime
        if (enabled)
        {
            var info = Json.Obj(status, "server");
            var used = Json.Num(status, "last_used") is { } lastUsed ? $"，最近一次在 {Format.Since(lastUsed)}前" : "";
            var facts = Facts(
                ("状态", $"{label} · {detail}", false),
                ("服务程序", Json.Str(info, "name") is { } serverName ? $"{serverName} {Json.Str(info, "version")}" : "", false),
                ("协议版本", Json.Str(status, "protocol") ?? "", true),
                ("进程", Json.Num(status, "pid") is { } pid ? $"{pid:0}" : "", true),
                ("工具调用", Json.Num(status, "calls") is > 0 and var calls ? $"{calls:0} 次{used}" : "", false),
                ("远程会话", Json.Num(status, "sessions") is > 0 and var sessions ? $"{sessions:0} 个" : "", false));
            var runtime = new StackPanel { Spacing = 12 };
            runtime.Children.Add(facts);
            if (Json.Str(status, "error") is { Length: > 0 } error)
            {
                runtime.Children.Add(Text(error, "ErrorCopyStyle"));
                var tail = Json.Arr(status, "stderr_tail").Select(l => l.GetString() ?? "").Where(l => l.Length > 0).ToList();
                if (tail.Count > 0)
                    runtime.Children.Add(CodeBlock(string.Join("\n", tail.TakeLast(12))));
            }
            var test = Named(ActionButton(busy ? "处理中…" : "测试", () => _ = TestMcpAsync(name)), $"测试 {name}");
            test.IsEnabled = !busy;
            var actions = ActionRow(test);
            if (!http && Json.Str(status, "state") is "running" or "error")
            {
                var restart = Named(ActionButton("重启", () => _ = RestartMcpAsync(name)), $"重启 {name}");
                restart.IsEnabled = !busy;
                actions.Children.Add(restart);
            }
            if (Json.Str(status, "log_file") is { } log)
                actions.Children.Add(Named(ActionButton("打开日志", () => OpenPath(log, select: true)), $"打开 {name} 的日志"));
            runtime.Children.Add(actions);
            page.Children.Add(Section("运行", http ? "请求直接转发到这个本机地址。" : "第一次被调用时启动，崩溃后下一次调用会自动重启。", Card(runtime)));
        }

        // ---- how it is started
        var env = Json.Obj(server, "env") is { ValueKind: JsonValueKind.Object } values
            ? string.Join("\n", values.EnumerateObject().Select(kv => $"{kv.Name}={kv.Value.GetString()}"))
            : "";
        var headers = Json.Obj(server, "headers") is { ValueKind: JsonValueKind.Object } headerValues
            ? string.Join("\n", headerValues.EnumerateObject().Select(kv => $"{kv.Name}: {kv.Value.GetString()}"))
            : "";
        var launch = http
            ? Facts(("地址", Json.Str(server, "url") ?? "", true), ("请求头", headers, true))
            : Facts(
                ("命令", Json.Str(server, "command") ?? "", true),
                ("参数", string.Join("\n", Json.Arr(server, "args").Select(a => a.GetString())), true),
                ("工作目录", Json.Str(server, "cwd") ?? "", true),
                ("环境变量", env, true),
                ("启动超时", $"{Json.Num(server, "startup_timeout_s") ?? 60:0} 秒", false),
                ("闲置停止", Json.Num(server, "idle_stop_minutes") is > 0 and var idle ? $"{idle:0} 分钟无调用后停止" : "", false));
        var edit = Named(ActionButton("编辑…", () => _ = EditMcpAsync(server)), $"编辑 {name}");
        page.Children.Add(Section("启动方式", http ? "本机已经以 HTTP 提供的 MCP 服务" : "winhand 用这条命令在本机启动它（stdio）",
            Card(new StackPanel { Spacing = 12, Children = { launch, ActionRow(edit) } })));

        // ---- removal, apart from everything else
        var delete = Named(ActionButton("删除…", () => _ = DeleteMcpAsync(name)), $"删除 {name}");
        page.Children.Add(RowsPanel(SettingRow("删除这个服务", "从 winhand 的配置里移除并停止它；服务本身的程序和数据不受影响。", delete)));
        return page;
    }

    private static Border CodeBlock(string text)
    {
        var block = Text(text, "DataCopyStyle");
        block.TextWrapping = TextWrapping.Wrap;
        return new Border
        {
            Child = block,
            Padding = new Thickness(12),
            CornerRadius = new CornerRadius(6),
            Background = Resource<Microsoft.UI.Xaml.Media.Brush>("ControlFillColorSecondaryBrush")
        };
    }

    private async Task RestartMcpAsync(string name)
    {
        if (!_mcpBusy.Add(name))
            return;
        RenderMcpDetail();
        try
        {
            await _backend.CallAsync("mcp_stop", new Dictionary<string, object?> { ["name"] = name });
            ApplyMcp(await _backend.CallAsync("mcp_start", new Dictionary<string, object?> { ["name"] = name }));
        }
        catch (Exception error)
        {
            ShowNotice($"{name} 没有重启成功", error.Message, InfoBarSeverity.Error);
        }
        finally
        {
            _mcpBusy.Remove(name);
            RenderMcp();
        }
    }

    // ---------------------------------------------------------------- test

    private async Task TestMcpAsync(string name)
    {
        if (!_mcpBusy.Add(name))
            return;
        RenderMcpDetail();
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
                    Text("正在单独启动一份这个服务并列出它的工具，不影响正在使用的那份。", "SecondaryCopyStyle")
                }
            },
            CloseButtonText = "完成",
            DefaultButton = ContentDialogButton.Close
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
        finally
        {
            _mcpBusy.Remove(name);
            RenderMcpDetail();
        }

        var report = new StackPanel { Spacing = 12, MinWidth = 480 };
        if (result is { } r && Json.Bool(r, "ok"))
        {
            var info = Json.Obj(r, "server");
            var tools = Json.Arr(r, "tools").ToList();
            report.Children.Add(new StackPanel
            {
                Orientation = Orientation.Horizontal,
                Spacing = 12,
                Children = { Pill("可以使用", Tone.Success), Text($"{Json.Str(info, "name")} {Json.Str(info, "version")} · 协议 {Json.Str(r, "protocol")} · 用时 {Format.Duration(Json.Num(r, "duration_ms"))}", "SecondaryCopyStyle") }
            });
            var extras = new List<string>();
            if (Json.Arr(r, "prompts").Count() is > 0 and var prompts)
                extras.Add($"{prompts} 个提示词");
            if (Json.Num(r, "resources") is > 0 and var resources)
                extras.Add($"{resources:0} 个资源");
            report.Children.Add(Text($"{tools.Count} 个工具" + (extras.Count > 0 ? $"，另有{string.Join("、", extras)}" : ""), "LabelCopyStyle"));
            var list = new StackPanel { Spacing = 8 };
            foreach (var tool in tools)
            {
                var row = new StackPanel { Spacing = 2 };
                var toolName = Text(Json.Str(tool, "name") ?? "", "DataCopyStyle");
                toolName.FontSize = 13;
                toolName.Foreground = Resource<Microsoft.UI.Xaml.Media.Brush>("TextFillColorPrimaryBrush");
                row.Children.Add(toolName);
                if (Json.Str(tool, "description") is { Length: > 0 } description)
                    row.Children.Add(Text(description, "SecondaryCopyStyle"));
                list.Children.Add(row);
            }
            report.Children.Add(list);
        }
        else
        {
            report.Children.Add(Pill("启动失败", Tone.Error));
            report.Children.Add(Text(failure ?? Json.Str(result, "error") ?? "未知错误", "ErrorCopyStyle"));
            var tail = Json.Arr(result, "stderr_tail").Select(l => l.GetString() ?? "").Where(l => l.Length > 0).ToList();
            if (tail.Count > 0)
                report.Children.Add(Section("服务的最后输出", "", CodeBlock(string.Join("\n", tail))));
        }
        progress.Content = new ScrollViewer { Content = report, MaxHeight = 480, Padding = new Thickness(0, 0, 16, 0) };
        await shown;
    }

    // ---------------------------------------------------------------- add / edit

    private async Task EditMcpAsync(JsonElement? server)
    {
        var original = Json.Str(server, "name");
        var isHttp = Json.Str(server, "kind") == "http";
        TextBox Field(string header, string text, string placeholder = "", bool multiline = false)
        {
            var box = new TextBox
            {
                Header = header,
                Text = text,
                PlaceholderText = placeholder,
                AcceptsReturn = multiline,
                TextWrapping = multiline ? TextWrapping.Wrap : TextWrapping.NoWrap,
                MinHeight = multiline ? 84 : 36
            };
            if (multiline)
                box.FontFamily = new Microsoft.UI.Xaml.Media.FontFamily("Consolas");
            return box;
        }
        var name = Field("名称", original ?? "", "pyocd-debug");
        name.Description = "只能用字母、数字、- 和 _；它会成为远程地址的最后一段。";
        var kind = new RadioButtons { Header = "启动方式", MaxColumns = 2, Items = { "本地命令（stdio）", "本机 HTTP 地址" }, SelectedIndex = isHttp ? 1 : 0 };
        var command = Field("命令", Json.Str(server, "command") ?? "", "uv、npx、python，或 exe 的完整路径");
        var args = Field("参数（每行一个）", string.Join("\r", Json.Arr(server, "args").Select(a => a.GetString())), "--directory\rD:\\Dev\\my-mcp\rrun\rmy-mcp", multiline: true);
        var cwd = Field("工作目录（可选）", Json.Str(server, "cwd") ?? "");
        var env = Field("环境变量（可选，每行 名称=值）", PairsText(Json.Obj(server, "env"), "="), "API_KEY=…", multiline: true);
        env.Description = "密钥类的值显示为 ••••••••，不修改就会原样保留。";
        var url = Field("地址", Json.Str(server, "url") ?? "", "http://127.0.0.1:8000/mcp");
        var headers = Field("请求头（可选，每行 名称: 值）", PairsText(Json.Obj(server, "headers"), ": "), "Authorization: Bearer …", multiline: true);
        var description = Field("说明（可选）", Json.Str(server, "description") ?? "");
        var timeout = new NumberBox { Header = "启动超时（秒）", Value = Json.Num(server, "startup_timeout_s") ?? 60, Minimum = 5, Maximum = 600, SpinButtonPlacementMode = NumberBoxSpinButtonPlacementMode.Compact };
        var idle = new NumberBox { Header = "闲置多久后停止（分钟，0 = 不停止）", Value = Json.Num(server, "idle_stop_minutes") ?? 0, Minimum = 0, Maximum = 1440, SpinButtonPlacementMode = NumberBoxSpinButtonPlacementMode.Compact };
        var timing = new Grid { ColumnSpacing = 12 };
        timing.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        timing.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        timing.Children.Add(timeout);
        Grid.SetColumn(idle, 1);
        timing.Children.Add(idle);
        var error = Text("", "ErrorCopyStyle");
        error.Visibility = Visibility.Collapsed;

        var stdioFields = new StackPanel { Spacing = 16, Children = { command, args, cwd, env, timing } };
        var httpFields = new StackPanel { Spacing = 16, Children = { url, headers } };
        void SyncKind()
        {
            stdioFields.Visibility = kind.SelectedIndex == 0 ? Visibility.Visible : Visibility.Collapsed;
            httpFields.Visibility = kind.SelectedIndex == 1 ? Visibility.Visible : Visibility.Collapsed;
        }
        kind.SelectionChanged += (_, _) => SyncKind();
        SyncKind();

        var form = new StackPanel { Spacing = 16, Width = 520, Children = { name, kind, stdioFields, httpFields, description, error } };
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
                    ["enabled"] = server is null || Json.Bool(server, "enabled")
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
                var saved = await _backend.CallAsync("mcp_save", parameters);
                _mcpSelection = name.Text.Trim();
                ApplyMcp(saved);
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
            Content = Text("会停止这个服务，并从 winhand 的配置里移除。claude.ai 里对应的连接器之后会提示找不到服务，可以一并删掉。服务本身的程序和数据不受影响。"),
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
        var panel = new StackPanel { Spacing = 16, Width = 520 };
        var boxes = new List<(CheckBox Box, string Name)>();
        panel.Children.Add(Text(candidates.Count == 0
            ? "没有在 Codex、Claude Desktop、Claude Code 的配置里找到 MCP 服务。"
            : "只把配置复制到 winhand；原来的客户端照常使用，之后两边互不影响。", "SecondaryCopyStyle"));
        foreach (var group in candidates.GroupBy(c => Json.Str(c, "source") ?? ""))
        {
            var list = new StackPanel { Spacing = 4 };
            foreach (var candidate in group)
            {
                var name = Json.Str(candidate, "name") ?? "";
                var already = Json.Bool(candidate, "already");
                var summary = Text(Json.Str(candidate, "summary") ?? "", "DataCopyStyle");
                summary.TextTrimming = TextTrimming.CharacterEllipsis;
                summary.TextWrapping = TextWrapping.NoWrap;
                var box = Named(new CheckBox
                {
                    IsChecked = !already,
                    IsEnabled = !already,
                    Content = new StackPanel { Spacing = 2, Children = { Text(already ? $"{name}（已在 winhand 里）" : name, "LabelCopyStyle"), summary } }
                }, name);
                boxes.Add((box, name));
                list.Children.Add(box);
            }
            panel.Children.Add(Section(group.Key, "", list));
        }
        var dialog = new ContentDialog
        {
            XamlRoot = Content.XamlRoot,
            Title = "从其他客户端导入",
            Content = new ScrollViewer { Content = panel, MaxHeight = 480, Padding = new Thickness(0, 0, 16, 0) },
            PrimaryButtonText = candidates.Count > 0 ? "导入选中的" : "",
            CloseButtonText = candidates.Count > 0 ? "取消" : "完成",
            DefaultButton = candidates.Count > 0 ? ContentDialogButton.Primary : ContentDialogButton.Close
        };
        if (await dialog.ShowAsync() != ContentDialogResult.Primary)
            return;
        var names = boxes.Where(b => b.Box.IsChecked == true && b.Box.IsEnabled).Select(b => b.Name).ToList();
        if (names.Count == 0)
            return;
        if (await RequestAsync("mcp_import", new Dictionary<string, object?> { ["names"] = names }) is { } result)
        {
            var added = Json.Arr(result, "added").Select(a => a.GetString()).ToList();
            _mcpSelection = added.FirstOrDefault() ?? _mcpSelection;
            ApplyMcp(result);
            ShowNotice(added.Count > 0 ? $"导入了 {added.Count} 个服务" : "没有新的服务",
                added.Count > 0 ? "可以先点“测试”确认能启动，再把远程地址添加到 claude.ai。" : "选中的服务已经在 winhand 里。",
                InfoBarSeverity.Success);
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
