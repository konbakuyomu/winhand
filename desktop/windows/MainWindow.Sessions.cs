using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace Winhand.Desktop;

public sealed partial class MainWindow
{
    private StackPanel? _sessionRows;

    private UIElement BuildSessions()
    {
        _sessionRows = new StackPanel { Spacing = 12, Padding = new Thickness(24), MaxWidth = 1100 };
        return new ScrollViewer { Content = _sessionRows };
    }

    private void RenderSessions()
    {
        if (_sessionRows is null)
            return;
        _sessionRows.Children.Clear();
        _sessionRows.Children.Add(Text(
            "Claude 通过 session_start 打开的终端、调试器、串口和网络控制台。每个会话的完整输出都记录在日志文件里。",
            "SecondaryCopyStyle"));
        if (_sessions.Count == 0)
        {
            _sessionRows.Children.Add(Card(Text("当前没有打开的会话。", "SecondaryCopyStyle")));
            return;
        }
        foreach (var session in _sessions)
            _sessionRows.Children.Add(SessionCard(session));
    }

    private UIElement SessionCard(JsonElement session)
    {
        var id = Json.Str(session, "id") ?? "";
        var state = Json.Str(session, "state") ?? "";
        var tone = state switch
        {
            "awaiting_input" or "idle" => Tone.Success,
            "running" => Tone.Active,
            "needs_user" or "blocked" => Tone.Warning,
            "exited" => Tone.Neutral,
            _ => Tone.Neutral
        };
        var label = state switch
        {
            "awaiting_input" => "等待输入",
            "running" => "运行中",
            "needs_user" => "需要你操作",
            "idle" => "空闲",
            "blocked" => "可能卡住",
            "exited" => "已退出",
            _ => state
        };

        var header = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 12 };
        header.Children.Add(Text(id, "SectionHeadingStyle"));
        header.Children.Add(Pill(label, tone));
        if (Json.Str(session, "profile") is { } profile)
            header.Children.Add(Pill(profile, Tone.Neutral));

        var target = Json.Str(session, "transport") switch
        {
            "serial" => $"串口 {Json.Str(session, "port")} @ {Json.Num(session, "baudrate")}",
            "tcp" => $"TCP {Json.Str(session, "host")}:{Json.Num(session, "port")}",
            var transport => $"{transport} · " + string.Join(" ", Json.Arr(session, "argv").Select(a => a.GetString()))
        };
        var info = new StackPanel { Spacing = 4 };
        info.Children.Add(header);
        info.Children.Add(Text(target, "DataCopyStyle"));
        if (Json.Str(session, "cwd") is { } cwd)
            info.Children.Add(Text($"目录：{cwd}", "SecondaryCopyStyle"));
        if (Json.Str(session, "reason") is { Length: > 0 } reason)
            info.Children.Add(Text(reason, "SecondaryCopyStyle"));
        var unread = Json.Num(session, "unread_chars") ?? 0;
        if (unread > 0)
            info.Children.Add(Text($"有 {unread:0} 个字符尚未被 Claude 读取", "SecondaryCopyStyle"));

        var log = Json.Str(session, "log_file");
        var actions = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8, VerticalAlignment = VerticalAlignment.Top };
        actions.Children.Add(ActionButton("打开日志", () => OpenPath(log, select: true)));
        var stop = ActionButton("停止", () => _ = StopSessionAsync(id));
        stop.IsEnabled = state != "exited";
        actions.Children.Add(stop);

        var grid = new Grid { ColumnSpacing = 16 };
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        grid.Children.Add(info);
        Grid.SetColumn(actions, 1);
        grid.Children.Add(actions);
        return Card(grid);
    }

    private async Task StopSessionAsync(string id)
    {
        var dialog = new ContentDialog
        {
            XamlRoot = Content.XamlRoot,
            Title = $"停止会话 {id}？",
            Content = "会结束这个会话里的程序（连同子进程）。Claude 如果还在用它，下一次调用会收到“会话不存在”。",
            PrimaryButtonText = "停止",
            CloseButtonText = "取消",
            DefaultButton = ContentDialogButton.Close
        };
        if (await dialog.ShowAsync() == ContentDialogResult.Primary)
            await RequestAsync("stop_session", new Dictionary<string, object?> { ["id"] = id });
    }
}
