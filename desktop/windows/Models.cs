using System.Text.Json;

namespace Winhand.Desktop;

internal enum Tone { Neutral, Success, Warning, Error, Active }

internal static class Json
{
    public static string? Str(JsonElement? value, string name) =>
        value is { ValueKind: JsonValueKind.Object } v && v.TryGetProperty(name, out var p) && p.ValueKind == JsonValueKind.String ? p.GetString() : null;

    public static double? Num(JsonElement? value, string name) =>
        value is { ValueKind: JsonValueKind.Object } v && v.TryGetProperty(name, out var p) && p.ValueKind == JsonValueKind.Number ? p.GetDouble() : null;

    public static bool Bool(JsonElement? value, string name) =>
        value is { ValueKind: JsonValueKind.Object } v && v.TryGetProperty(name, out var p) && p.ValueKind == JsonValueKind.True;

    public static JsonElement? Obj(JsonElement? value, string name) =>
        value is { ValueKind: JsonValueKind.Object } v && v.TryGetProperty(name, out var p) && p.ValueKind != JsonValueKind.Null ? p : null;

    public static IEnumerable<JsonElement> Arr(JsonElement? value, string name) =>
        Obj(value, name) is { ValueKind: JsonValueKind.Array } array ? array.EnumerateArray() : [];

    public static string Pretty(JsonElement? value) =>
        value is { } v ? JsonSerializer.Serialize(v, new JsonSerializerOptions
        {
            WriteIndented = true,
            Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping
        }) : "";
}

internal static class Format
{
    public static DateTime Local(double unixSeconds) =>
        DateTimeOffset.FromUnixTimeMilliseconds((long)(unixSeconds * 1000)).LocalDateTime;

    public static string Since(double? unixSeconds)
    {
        if (unixSeconds is not { } t)
            return "";
        var span = DateTime.Now - Local(t);
        if (span.TotalSeconds < 60) return $"{Math.Max(0, (int)span.TotalSeconds)} 秒";
        if (span.TotalMinutes < 60) return $"{(int)span.TotalMinutes} 分钟";
        if (span.TotalHours < 24) return $"{(int)span.TotalHours} 小时 {span.Minutes} 分钟";
        return $"{(int)span.TotalDays} 天 {span.Hours} 小时";
    }

    public static string Duration(double? ms) => ms switch
    {
        null => "",
        < 1000 => $"{ms:0} ms",
        < 60_000 => $"{ms / 1000:0.0} s",
        _ => $"{ms / 60_000:0.0} min"
    };
}

/// <summary>How a relay connection state reads to a person.</summary>
internal sealed record StatusView(string Label, string Detail, Tone Tone, bool Online)
{
    public static StatusView From(JsonElement? status, bool backendRunning, string? backendError)
    {
        if (!backendRunning)
            return new("后端未运行", backendError ?? "正在启动 winhand 后端…", Tone.Error, false);
        var state = Json.Str(status, "state") ?? "starting";
        var relay = Json.Str(status, "relay") ?? "中转";
        var reason = Json.Str(status, "reason") ?? "";
        var retry = Json.Num(status, "retry_in_s");
        var connecting = Json.Bool(status, "connecting");
        return state switch
        {
            "online" => new("已连接", $"{relay} · 已在线 {Format.Since(Json.Num(status, "since"))}", Tone.Success, true),
            "connecting" => new("连接中", $"正在连接 {relay}…", Tone.Warning, false),
            "offline" => new("已断开",
                (connecting ? "正在重试… " : retry is { } r ? $"{r:0} 秒后重试 · " : "") + reason, Tone.Warning, false),
            "refused" => new("令牌被拒绝", "中转拒绝了设备令牌，请在设置里更新。" + (reason.Length > 0 ? $"（{reason}）" : ""), Tone.Error, false),
            "replaced" => new("已被接管", "另一台机器或另一个 winhand 连上了同一个中转。点“重新连接”夺回。", Tone.Error, false),
            "conflict" => new("有另一个实例", "终端里的 winhand connect 仍在运行，关掉它后点“重新连接”。", Tone.Error, false),
            "unconfigured" => new("未配置", "在设置里填写中转地址和设备令牌。", Tone.Neutral, false),
            "paused" => new("已暂停", "已手动断开，Claude 暂时无法使用这台电脑。", Tone.Neutral, false),
            _ => new("启动中", "正在准备…", Tone.Neutral, false)
        };
    }
}

internal sealed class ActivityEntry(JsonElement data)
{
    public JsonElement Data { get; private set; } = data;
    public string Id => Json.Str(Data, "id") ?? "";
    public string Kind => Json.Str(Data, "kind") ?? "tool";
    public string Title => Json.Str(Data, "title") ?? "";
    public string Status => Json.Str(Data, "status") ?? "";
    public string Summary => Json.Str(Data, "summary") ?? "";
    public double Time => Json.Num(Data, "t") ?? 0;
    public double? DurationMs => Json.Num(Data, "duration_ms");
    public JsonElement? Args => Json.Obj(Data, "args");

    public void Update(JsonElement data) => Data = data;

    public bool IsError => Status is "error" or "refused" or "replaced" or "conflict";

    public Tone Tone => Status switch
    {
        "ok" or "online" => Tone.Success,
        "running" => Tone.Active,
        "offline" or "paused" or "unconfigured" => Tone.Warning,
        _ when IsError => Tone.Error,
        _ => Tone.Neutral
    };

    public string StatusLabel => Status switch
    {
        "ok" => "完成",
        "running" => "进行中",
        "error" => "失败",
        "online" => "在线",
        "offline" => "断开",
        "refused" => "被拒绝",
        "replaced" => "被接管",
        "conflict" => "冲突",
        "paused" => "暂停",
        "unconfigured" => "未配置",
        _ => Status
    };

    public string KindLabel => Kind switch { "tool" => "工具", "connection" => "连接", "session" => "会话", _ => Kind };

    /// <summary>The one argument that best says what the call did (command, path, text…).</summary>
    public string ArgsLine
    {
        get
        {
            if (Args is not { ValueKind: JsonValueKind.Object } args)
                return "";
            foreach (var key in new[] { "command", "text", "path", "profile", "ids", "id", "pattern", "patterns", "keys" })
            {
                if (args.TryGetProperty(key, out var value) && value.ValueKind != JsonValueKind.Null)
                    return $"{key}: {Flatten(value)}";
            }
            var first = args.EnumerateObject().FirstOrDefault();
            return first.Value.ValueKind == JsonValueKind.Undefined ? "" : $"{first.Name}: {Flatten(first.Value)}";
        }
    }

    private static string Flatten(JsonElement value)
    {
        var text = value.ValueKind == JsonValueKind.String ? value.GetString() ?? "" : value.GetRawText();
        text = text.Replace("\r", " ").Replace("\n", " ⏎ ");
        return text.Length > 140 ? text[..140] + "…" : text;
    }
}
