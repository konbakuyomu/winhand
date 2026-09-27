using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace Winhand.Desktop;

/// <summary>The "会话" page: terminals, debuggers, serial and network consoles Claude opened with
/// session_start. The list on the left, the selected session's state and actions on the right.</summary>
public sealed partial class MainWindow
{
    private ListView? _sessionList;
    private ContentControl? _sessionDetail;
    private string? _sessionSelection;
    private bool _renderingSessions;
    // what the detail pane was built for, and the texts that change without a rebuild
    private string? _sessionDetailShape;
    private ContentControl? _sessionPill;
    private Dictionary<string, TextBlock> _sessionFacts = new();

    private UIElement BuildSessions()
    {
        _sessionList = new ListView { SelectionMode = ListViewSelectionMode.Single };
        Named(_sessionList, "会话");
        _sessionList.SelectionChanged += (_, _) =>
        {
            if (_renderingSessions || _sessionList.SelectedItem is not ListViewItem { Tag: string id })
                return;
            _sessionSelection = id;
            RenderSessionDetail();
        };
        _sessionDetail = new ContentControl { HorizontalContentAlignment = HorizontalAlignment.Stretch, VerticalContentAlignment = VerticalAlignment.Stretch };
        RenderSessions();
        return SplitWorkspace("sessions", ListPane(null, _sessionList), _sessionDetail, 260, 220, 360);
    }

    private static (string Label, Tone Tone) SessionState(string state) => state switch
    {
        "awaiting_input" => ("等待输入", Tone.Success),
        "running" => ("运行中", Tone.Active),
        "needs_user" => ("需要你操作", Tone.Warning),
        "idle" => ("空闲", Tone.Success),
        "blocked" => ("可能卡住", Tone.Warning),
        "exited" => ("已退出", Tone.Neutral),
        _ => (state, Tone.Neutral)
    };

    private static string SessionTarget(JsonElement session) => Json.Str(session, "transport") switch
    {
        "serial" => $"串口 {Json.Str(session, "port")} @ {Json.Num(session, "baudrate")}",
        "tcp" => $"TCP {Json.Str(session, "host")}:{Json.Num(session, "port")}",
        _ => string.Join(" ", Json.Arr(session, "argv").Select(a => a.GetString()))
    };

    private void RenderSessions()
    {
        if (_sessionList is null)
            return;
        _renderingSessions = true;
        // live sessions first, newest first within each group
        var ordered = _sessions.AsEnumerable().Reverse().OrderBy(s => Json.Str(s, "state") == "exited").ToList();
        if (_sessionSelection is null || ordered.All(s => Json.Str(s, "id") != _sessionSelection))
            _sessionSelection = ordered.Select(s => Json.Str(s, "id")).FirstOrDefault();
        var ids = ordered.Select(s => Json.Str(s, "id") ?? "").ToList();
        var shown = _sessionList.Items.OfType<ListViewItem>().Select(i => i.Tag as string ?? "").ToList();
        if (ids.SequenceEqual(shown))
        {
            // the backend reports live sessions every second: update the rows, do not rebuild them
            for (var i = 0; i < ordered.Count; i++)
            {
                var (label, tone) = SessionState(Json.Str(ordered[i], "state") ?? "");
                UpdateListRow((ListViewItem)_sessionList.Items[i], ids[i], $"{label} · {Json.Str(ordered[i], "transport")}", tone);
            }
        }
        else
        {
            _sessionList.Items.Clear();
            foreach (var session in ordered)
            {
                var id = Json.Str(session, "id") ?? "";
                var (label, tone) = SessionState(Json.Str(session, "state") ?? "");
                _sessionList.Items.Add(ListRow(id, id, $"{label} · {Json.Str(session, "transport")}", tone));
            }
        }
        var selected = _sessionList.Items.OfType<ListViewItem>().FirstOrDefault(i => i.Tag as string == _sessionSelection);
        if (!ReferenceEquals(_sessionList.SelectedItem, selected))
            _sessionList.SelectedItem = selected;
        _renderingSessions = false;
        RenderSessionDetail();
    }

    private void RenderSessionDetail()
    {
        if (_sessionDetail is null)
            return;
        var page = PagePanel();
        var session = _sessions.FirstOrDefault(s => Json.Str(s, "id") == _sessionSelection);
        if (session.ValueKind != JsonValueKind.Object)
        {
            page.Children.Add(EmptyState("没有会话",
                "Claude 用 session_start 打开终端、调试器、串口或网络控制台时，会话会出现在这里。每个会话的完整输出都记录在日志文件里。"));
            _sessionDetail.Content = DetailPane(page);
            _sessionDetailShape = null;
            _sessionPill = null;
            return;
        }

        var id = Json.Str(session, "id") ?? "";
        var state = Json.Str(session, "state") ?? "";
        var (label, tone) = SessionState(state);
        var profile = Json.Str(session, "profile");
        var unread = Json.Num(session, "unread_chars") ?? 0;
        (string Label, string Value, bool Mono)[] rows =
        [
            ("状态", Json.Str(session, "reason") ?? label, false),
            ("方式", Json.Str(session, "transport") ?? "", false),
            ("目标", SessionTarget(session), true),
            ("目录", Json.Str(session, "cwd") ?? "", true),
            ("进程", Json.Num(session, "pid") is { } pid ? $"{pid:0}" : "", true),
            ("未读输出", unread > 0 ? $"{unread:0} 个字符尚未被 Claude 读取" : "", false),
            ("日志", Json.Str(session, "log_file") ?? "", true)
        ];

        // Same session, same state, same rows: only texts changed (the "stuck for Ns" count
        // ticks every second). Update them in place so the page neither jumps nor loses its scroll.
        var shape = $"{id}|{state == "exited"}|{profile}|" + string.Join(",", rows.Where(r => r.Value.Length > 0).Select(r => r.Label));
        if (shape == _sessionDetailShape && _sessionPill is not null)
        {
            _sessionPill.Content = label;
            _sessionPill.Style = PillStyle(tone);
            foreach (var (name, value, _) in rows)
            {
                if (_sessionFacts.TryGetValue(name, out var text) && text.Text != value)
                    text.Text = value;
            }
            return;
        }
        _sessionDetailShape = shape;
        _sessionPill = Pill(label, tone);
        page.Children.Add(DetailHeader(id, profile is null ? "" : $"profile：{profile}", _sessionPill, null));
        var facts = Facts(rows);
        _sessionFacts = FactValues(facts);
        var actions = ActionRow();
        if (Json.Str(session, "log_file") is { } log)
            actions.Children.Add(Named(ActionButton("打开日志", () => OpenPath(log, select: true)), $"打开 {id} 的日志"));
        if (state == "exited")
            actions.Children.Add(Named(ActionButton("移出列表", () => _ = ForgetSessionAsync(id)), $"把 {id} 移出列表"));
        else
            actions.Children.Add(Named(ActionButton("停止…", () => _ = StopSessionAsync(id)), $"停止 {id}"));
        page.Children.Add(Card(new StackPanel { Spacing = 12, Children = { facts, actions } }));
        _sessionDetail.Content = DetailPane(page);
    }

    private async Task ForgetSessionAsync(string id)
    {
        await RequestAsync("stop_session", new Dictionary<string, object?> { ["id"] = id, ["force"] = true });
    }

    private async Task StopSessionAsync(string id)
    {
        var dialog = new ContentDialog
        {
            XamlRoot = Content.XamlRoot,
            Title = $"停止会话 {id}？",
            Content = Text("会结束这个会话里的程序（连同子进程）。Claude 如果还在用它，下一次调用会收到“会话不存在”。"),
            PrimaryButtonText = "停止",
            CloseButtonText = "取消",
            DefaultButton = ContentDialogButton.Close
        };
        if (await dialog.ShowAsync() == ContentDialogResult.Primary)
            await RequestAsync("stop_session", new Dictionary<string, object?> { ["id"] = id });
    }
}
