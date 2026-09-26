using System.Collections.Concurrent;
using System.Diagnostics;
using System.Text;
using System.Text.Json;

namespace Winhand.Desktop;

internal sealed record BackendEvent(string Name, JsonElement Data);

internal sealed class BackendException(string code, string message) : Exception(message)
{
    public string Code { get; } = code;
}

/// <summary>Runs `winhand.exe desktop-backend` and talks to it in JSON lines (desktop/PROTOCOL.md).
/// The backend lives exactly as long as this client: closing its stdin makes it shut down.</summary>
internal sealed class BackendClient : IAsyncDisposable
{
    private readonly SemaphoreSlim _writeGate = new(1, 1);
    private readonly ConcurrentDictionary<int, TaskCompletionSource<JsonElement>> _pending = new();
    private Process? _process;
    private StreamWriter? _writer;
    private int _nextId;
    private string? _generation;

    public event EventHandler<BackendEvent>? EventReceived;
    public event EventHandler<string>? Disconnected;

    public bool IsRunning => _process is { HasExited: false };
    public string Executable { get; private set; } = "";

    public async Task<JsonElement> StartAsync(CancellationToken cancellationToken)
    {
        await StopAsync(sendShutdown: false);
        var (executable, arguments) = ResolveLaunch();
        Executable = executable;
        var startInfo = new ProcessStartInfo
        {
            FileName = executable,
            // not the install folder: whatever Claude starts inherits this, and a process
            // sitting in the install folder would stop updates from replacing it
            WorkingDirectory = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
            UseShellExecute = false,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
            StandardInputEncoding = new UTF8Encoding(false),
            StandardOutputEncoding = new UTF8Encoding(false),
            StandardErrorEncoding = new UTF8Encoding(false)
        };
        foreach (var argument in arguments)
            startInfo.ArgumentList.Add(argument);
        startInfo.ArgumentList.Add("desktop-backend");

        var process = new Process { StartInfo = startInfo, EnableRaisingEvents = true };
        process.Exited += (_, _) => OnExited(process);
        try
        {
            process.Start();
        }
        catch (Exception error)
        {
            throw new BackendException("launch_failed", $"无法启动后端 {executable}：{error.Message}");
        }
        _process = process;
        _writer = process.StandardInput;
        _ = ReadAsync(process);
        _ = DrainAsync(process);

        var result = await CallAsync("initialize", new Dictionary<string, object?> { ["protocol_version"] = 1 }, cancellationToken);
        _generation = result.TryGetProperty("generation", out var generation) ? generation.GetString() : null;
        if (string.IsNullOrEmpty(_generation))
            throw new BackendException("protocol", "后端未返回 generation");
        return result;
    }

    public async Task<JsonElement> CallAsync(string method, object? parameters = null, CancellationToken cancellationToken = default)
    {
        var writer = _writer;
        if (_process is not { HasExited: false } || writer is null)
            throw new BackendException("disconnected", "后端未运行");
        var id = Interlocked.Increment(ref _nextId);
        var reply = new TaskCompletionSource<JsonElement>(TaskCreationOptions.RunContinuationsAsynchronously);
        _pending[id] = reply;
        try
        {
            using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            timeout.CancelAfter(TimeSpan.FromSeconds(30));
            var line = JsonSerializer.Serialize(new Dictionary<string, object?>
            {
                ["id"] = id,
                ["method"] = method,
                ["params"] = parameters ?? new Dictionary<string, object?>()
            });
            await _writeGate.WaitAsync(timeout.Token);
            try
            {
                await writer.WriteLineAsync(line.AsMemory(), timeout.Token);
                await writer.FlushAsync(timeout.Token);
            }
            finally
            {
                _writeGate.Release();
            }
            return await reply.Task.WaitAsync(timeout.Token);
        }
        finally
        {
            _pending.TryRemove(id, out _);
        }
    }

    public async Task StopAsync(bool sendShutdown = true)
    {
        var process = _process;
        if (process is null)
            return;
        _process = null;
        if (sendShutdown && !process.HasExited)
        {
            try
            {
                _process = process;
                using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(12));
                await CallAsync("shutdown", null, timeout.Token);
            }
            catch (Exception)
            {
                // fall through: closing stdin or killing the tree below still ends it
            }
            finally
            {
                _process = null;
            }
        }
        try { _writer?.Close(); } catch (Exception) { }
        _writer = null;
        if (!process.HasExited)
        {
            try
            {
                await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(8));
            }
            catch (TimeoutException)
            {
                process.Kill(entireProcessTree: true);
            }
        }
        _generation = null;
        process.Dispose();
    }

    private async Task ReadAsync(Process process)
    {
        try
        {
            while (await process.StandardOutput.ReadLineAsync() is { } line)
            {
                if (!string.IsNullOrWhiteSpace(line))
                    HandleLine(line);
            }
        }
        catch (Exception)
        {
            // exit is reported through OnExited
        }
    }

    private static async Task DrainAsync(Process process)
    {
        try
        {
            while (await process.StandardError.ReadLineAsync() is not null) { }
        }
        catch (Exception) { }
    }

    private void HandleLine(string line)
    {
        JsonElement root;
        try
        {
            using var document = JsonDocument.Parse(line);
            root = document.RootElement.Clone();
        }
        catch (JsonException)
        {
            return;
        }
        if (root.TryGetProperty("id", out var idElement) && idElement.TryGetInt32(out var id))
        {
            if (!_pending.TryGetValue(id, out var waiting))
                return;
            if (root.TryGetProperty("error", out var error))
                waiting.TrySetException(new BackendException(
                    error.TryGetProperty("code", out var code) ? code.GetString() ?? "error" : "error",
                    error.TryGetProperty("message", out var message) ? message.GetString() ?? "" : ""));
            else if (root.TryGetProperty("result", out var result))
                waiting.TrySetResult(result);
            return;
        }
        if (!root.TryGetProperty("event", out var name) || !root.TryGetProperty("data", out var data))
            return;
        if (_generation is not null && data.TryGetProperty("generation", out var generation) && generation.GetString() != _generation)
            return; // output of an older backend
        EventReceived?.Invoke(this, new BackendEvent(name.GetString() ?? "", data));
    }

    private void OnExited(Process process)
    {
        foreach (var waiting in _pending.Values)
            waiting.TrySetException(new BackendException("disconnected", "后端已退出"));
        if (ReferenceEquals(_process, process))
        {
            _process = null;
            Disconnected?.Invoke(this, $"后端已退出（代码 {SafeExitCode(process)}）");
        }
    }

    private static string SafeExitCode(Process process)
    {
        try { return process.ExitCode.ToString(); } catch (Exception) { return "?"; }
    }

    /// <summary>The packaged backend sits next to the app. For development point
    /// WINHAND_BACKEND_PATH at another executable, e.g. `uv` with
    /// WINHAND_BACKEND_ARGS="run --project D:\...\winhand\agent winhand".</summary>
    private static (string Executable, string[] Arguments) ResolveLaunch()
    {
        var configured = Environment.GetEnvironmentVariable("WINHAND_BACKEND_PATH");
        if (!string.IsNullOrWhiteSpace(configured))
        {
            var extra = Environment.GetEnvironmentVariable("WINHAND_BACKEND_ARGS") ?? "";
            return (configured, extra.Split(' ', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries));
        }
        var bundled = Path.Combine(AppContext.BaseDirectory, "backend", "winhand.exe");
        if (!File.Exists(bundled))
            throw new BackendException("missing_backend", $"找不到后端程序：{bundled}");
        return (bundled, []);
    }

    public async ValueTask DisposeAsync()
    {
        await StopAsync();
        _writeGate.Dispose();
    }
}
