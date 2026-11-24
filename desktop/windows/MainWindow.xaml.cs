using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using WinRT.Interop;

namespace Winhand.Desktop;

public sealed partial class MainWindow : Window
{
    private const int SwHide = 0, SwShow = 5, SwRestore = 9;

    private readonly BackendClient _backend = new();
    private readonly AppUpdater _updater = new();
    private readonly NativeTray _tray;
    private readonly AppWindow _appWindow;
    private readonly nint _hwnd;
    private readonly Microsoft.UI.Dispatching.DispatcherQueueTimer _ticker;
    private readonly Dictionary<string, UIElement> _pages = [];
    private JsonElement? _status, _paths, _relay, _counts;
    private List<JsonElement> _sessions = [];
    private string? _backendVersion, _backendError;
    private string _currentPage = "overview";
    private int _restartAttempts;
    private bool _starting, _shuttingDown, _allowClose, _everShown;
    private const string BackendFailedTitle = "后端启动失败";

    public MainWindow()
    {
        InitializeComponent();
        if (!string.IsNullOrEmpty(Environment.GetEnvironmentVariable("WINHAND_DESKTOP_INSTANCE")))
        {
            if (Environment.GetEnvironmentVariable("WINHAND_DESKTOP_THEME") == "light")
                RootGrid.RequestedTheme = ElementTheme.Light;
            else if (Environment.GetEnvironmentVariable("WINHAND_DESKTOP_THEME") == "dark")
                RootGrid.RequestedTheme = ElementTheme.Dark;
        }
        ExtendsContentIntoTitleBar = true;
        SetTitleBar(TitleBarDragRegion);
        RootNavigation.RegisterPropertyChangedCallback(NavigationView.IsPaneOpenProperty, (_, _) => UpdatePaneFooter());
        RootNavigation.Loaded += (_, _) =>
        {
            // The native template puts PaneFooter above FooterMenuItems; the design puts it below.
            // These are the template's own parts: when another WinUI version lays them out differently,
            // the native order stays rather than the app failing to start.
            DependencyObject? footer = MascotStatusButton;
            while (footer is not null && (footer as FrameworkElement)?.Name != "FooterContentBorder")
                footer = VisualTreeHelper.GetParent(footer);
            if (footer is FrameworkElement border && VisualTreeHelper.GetParent(border) is Grid items
                && items.Children.OfType<FrameworkElement>().Where(item => Grid.GetRow(item) == 3).ToList() is [var footerItems])
            {
                Grid.SetRow(footerItems, 2);
                Grid.SetRow(border, 3);
            }
        };
        UpdatePaneFooter();
        _hwnd = WindowNative.GetWindowHandle(this);
        _appWindow = AppWindow.GetFromWindowId(Microsoft.UI.Win32Interop.GetWindowIdFromWindow(_hwnd));
        _appWindow.Title = "winhand";
        _appWindow.SetIcon(Path.Combine(AppContext.BaseDirectory, "Assets", "winhand.ico"));
        var scale = GetDpiForWindow(_hwnd) / 96.0; // AppWindow sizes are physical pixels
        var width = 1180;
        var height = 760;
        if (!string.IsNullOrEmpty(Environment.GetEnvironmentVariable("WINHAND_DESKTOP_INSTANCE"))
            && Environment.GetEnvironmentVariable("WINHAND_DESKTOP_SIZE")?.Split('x') is [var w, var h]
            && int.TryParse(w, out var previewWidth) && previewWidth > 0
            && int.TryParse(h, out var previewHeight) && previewHeight > 0)
        {
            width = previewWidth;
            height = previewHeight;
        }
        _appWindow.Resize(new Windows.Graphics.SizeInt32((int)(width * scale), (int)(height * scale)));
        _appWindow.Closing += OnAppWindowClosing;

        _tray = new NativeTray(_hwnd, ShowMainWindow, TrayMenu);
        _tray.Show();

        _backend.EventReceived += (_, e) => DispatcherQueue.TryEnqueue(() => OnBackendEvent(e));
        _backend.Disconnected += (_, message) => DispatcherQueue.TryEnqueue(() => OnBackendDisconnected(message));

        InitializeFeedback();
        BuildPages();
        ShowPage("overview", fromNavigation: true); // the XAML already selects it; setting it again misplaces the indicator
        if (!string.IsNullOrEmpty(Environment.GetEnvironmentVariable("WINHAND_DESKTOP_INSTANCE"))
            && Environment.GetEnvironmentVariable("WINHAND_DESKTOP_PAGE") is { } page && _pages.ContainsKey(page))
            RootNavigation.Loaded += (_, _) => ShowPage(page); // selected before Loaded, the XAML-selected item stays highlighted too
        if (PreviewState("collapsed"))
            RootNavigation.Loaded += (_, _) => RootNavigation.IsPaneOpen = false;
        // Check that an error escaping a XAML callback is logged and survived (CrashGuard).
        if (Environment.GetEnvironmentVariable("WINHAND_DESKTOP_SELFTEST") == "ui-error")
            RootNavigation.Loaded += (_, _) => throw new ArgumentException("WINHAND_DESKTOP_SELFTEST=ui-error");
        _ticker = DispatcherQueue.CreateTimer();
        _ticker.Interval = TimeSpan.FromSeconds(1);
        _ticker.Tick += (_, _) => RefreshStatus();
        _ticker.Start();
        RefreshStatus();
        DispatcherQueue.TryEnqueue(async () => await StartBackendAsync());
        StartUpdateChecks();
    }

    // ------------------------------------------------------------------ backend

    private async Task StartBackendAsync()
    {
        if (_starting || _shuttingDown)
            return;
        _starting = true;
        try
        {
            var snapshot = await _backend.StartAsync(CancellationToken.None);
            _backendError = null;
            _restartAttempts = 0;
            ApplySnapshot(snapshot);
            if (_noticeTitle == BackendFailedTitle)
                HideNotice(); // only its own notice: one about a caught UI error stays
        }
        catch (Exception error)
        {
            _backendError = error.Message;
            ShowNotice(BackendFailedTitle, error.Message, InfoBarSeverity.Error);
            ScheduleRestart();
        }
        finally
        {
            _starting = false;
            RefreshStatus();
        }
    }

    private void OnBackendDisconnected(string message)
    {
        if (_shuttingDown)
            return;
        _backendError = message;
        HideSecret();
        RefreshStatus();
        ScheduleRestart();
    }

    /// <summary>The backend should never die; if it does, bring it back with growing delays.</summary>
    private void ScheduleRestart()
    {
        if (_shuttingDown)
            return;
        var delay = TimeSpan.FromSeconds(Math.Min(30, Math.Pow(2, _restartAttempts++)));
        var timer = DispatcherQueue.CreateTimer();
        timer.Interval = delay;
        timer.IsRepeating = false;
        timer.Tick += async (_, _) => await StartBackendAsync();
        timer.Start();
    }

    private void ApplySnapshot(JsonElement snapshot)
    {
        _status = Json.Obj(snapshot, "status");
        _paths = Json.Obj(snapshot, "paths");
        _relay = Json.Obj(snapshot, "relay");
        _counts = Json.Obj(snapshot, "counts");
        _backendVersion = Json.Str(snapshot, "version");
        _sessions = Json.Arr(snapshot, "sessions").ToList();
        ApplyMcp(Json.Obj(snapshot, "mcp"));
        ApplyApps(Json.Obj(snapshot, "apps"));
        ApplySecrets(Json.Obj(snapshot, "secrets"));
        LoadActivity(Json.Arr(snapshot, "activity"));
        RenderSessions();
        RenderSettings();
        RenderOverview();
    }

    private void OnBackendEvent(BackendEvent e)
    {
        switch (e.Name)
        {
            case "status":
                _status = e.Data;
                RefreshStatus();
                break;
            case "activity":
                UpsertActivity(e.Data);
                break;
            case "sessions":
                _sessions = Json.Arr(e.Data, "sessions").ToList();
                RenderSessions();
                RenderOverview();
                break;
            case "mcp":
                ApplyMcp(e.Data);
                break;
            case "apps":
                ApplyApps(e.Data);
                break;
            case "secrets":
                ApplySecrets(e.Data);
                break;
        }
    }

    private async Task<JsonElement?> RequestAsync(string method, object? parameters = null)
    {
        try
        {
            return await _backend.CallAsync(method, parameters);
        }
        catch (Exception error)
        {
            ShowNotice("操作失败", error.Message, InfoBarSeverity.Error);
            return null;
        }
    }

    private async Task ReconnectAsync()
    {
        if (!_backend.IsRunning)
        {
            _restartAttempts = 0;
            await StartBackendAsync();
            return;
        }
        if (await RequestAsync("reconnect") is { } result)
        {
            _status = Json.Obj(result, "status");
            RefreshStatus();
        }
    }

    private async Task DisconnectAsync()
    {
        if (await RequestAsync("disconnect") is { } result)
        {
            _status = Json.Obj(result, "status");
            RefreshStatus();
        }
    }

    // ------------------------------------------------------------------- status

    private StatusView CurrentStatus => StatusView.From(_status, _backend.IsRunning, _backendError);

    private void RefreshStatus()
    {
        var view = CurrentStatus;
        ConnectionDot.Style = CompactConnectionDot.Style = DotStyle(view.Tone);
        ConnectionLabel.Text = view.Label;
        ConnectionLabel.Style = Resource<Style>("ConnectionLabel" + (view.Tone switch
        {
            Tone.Success => "Success",
            Tone.Warning => "Warning",
            Tone.Error => "Critical",
            Tone.Active => "Active",
            _ => "Neutral"
        }) + "Style");
        var version = System.Reflection.Assembly.GetExecutingAssembly().GetName().Version?.ToString(3) ?? "?";
        ConnectionMeta.Text = $"v{version}" + (view.Online ? $" · {Format.Since(Json.Num(_status, "since"))}" : "");
        ConnectionToolTip.Content = $"{Json.Str(_relay, "url")}\n{view.Label} · {view.Detail}";
        var openSessions = _sessions.Count(s => Json.Str(s, "state") != "exited");
        NavSessions.Visibility = openSessions > 0 || _currentPage == "sessions" ? Visibility.Visible : Visibility.Collapsed;
        SessionsBadge.Value = openSessions;
        SessionsBadge.Style = Resource<Style>(_sessions.Any(s => Json.Str(s, "state") is "needs_user" or "blocked")
            ? "WinhandSessionCautionBadgeStyle" : "WinhandSessionBadgeStyle");
        UpdateBadge.Visibility = _updater.Available || _updater.Ready ? Visibility.Visible : Visibility.Collapsed;
        var update = _updater.Ready ? $"\n新版本 {_updater.LatestVersion} 已就绪"
            : _updater.Available ? $"\n发现新版本 {_updater.LatestVersion}" : "";
        _tray.Update(view.Online, $"winhand · {view.Label}\n{view.Detail}{update}");
        RenderOverviewStatus(view);
        RenderRelayStatus(view);
        RefreshRunningDurations();
    }

    // -------------------------------------------------------------- navigation

    private void OnPaneToggleClick(object sender, RoutedEventArgs args) => RootNavigation.IsPaneOpen = !RootNavigation.IsPaneOpen;

    private void OnMascotStatusClick(object sender, RoutedEventArgs args) => ShowPage("overview");

    private void OnRootSizeChanged(object sender, SizeChangedEventArgs args)
    {
        if ((args.PreviousSize.Width < 680) != (args.NewSize.Width < 680))
            RootNavigation.IsPaneOpen = args.NewSize.Width >= 680;
    }

    private void UpdatePaneFooter()
    {
        MascotExpandedGrid.Visibility = RootNavigation.IsPaneOpen ? Visibility.Visible : Visibility.Collapsed;
        CompactConnectionDot.Visibility = RootNavigation.IsPaneOpen ? Visibility.Collapsed : Visibility.Visible;
    }

    private void OnNavigationSelectionChanged(NavigationView sender, NavigationViewSelectionChangedEventArgs args)
    {
        if (args.SelectedItem is NavigationViewItem { Tag: string tag })
            ShowPage(tag, fromNavigation: true);
    }

    private void ShowPage(string page, bool fromNavigation = false)
    {
        _currentPage = page;
        if (page != "secrets")
            HideSecret();
        PageHost.Content = _pages[page];
        if (page == "apps")
            _ = RefreshAppsAsync(); // `winhand apps` or an edited file changes them without an event
        if (page == "secrets")
            _ = RefreshSecretsAsync(); // so does editing secrets.env by hand
        if (!fromNavigation)
            RootNavigation.SelectedItem = RootNavigation.MenuItems.Concat(RootNavigation.FooterMenuItems)
                .OfType<NavigationViewItem>().First(i => (string)i.Tag == page);
        NavSessions.Visibility = _sessions.Any(s => Json.Str(s, "state") != "exited") || page == "sessions"
            ? Visibility.Visible : Visibility.Collapsed;
    }

    private void BuildPages()
    {
        _pages["overview"] = PageHeader("概览", BuildOverview());
        _pages["activity"] = PageHeader("活动", BuildActivity());
        _pages["sessions"] = PageHeader("会话", BuildSessions());
        _pages["mcp"] = PageHeader("MCP 服务", BuildMcp());
        _pages["secrets"] = BuildSecretsSection();
        _pages["apps"] = BuildAppsSection();
        _pages["relay"] = BuildRelay();
        _pages["settings"] = BuildSettings();
    }

    // ------------------------------------------------------------ window/tray

    internal void ShowMainWindow()
    {
        if (!string.IsNullOrEmpty(Environment.GetEnvironmentVariable("WINHAND_DESKTOP_INSTANCE"))
            && !string.IsNullOrEmpty(Environment.GetEnvironmentVariable("WINHAND_DESKTOP_PAGE")))
        {
            _everShown = true;
            _appWindow.Show(false);
            return;
        }
        if (!_everShown)
        {
            _everShown = true;
            Activate();
        }
        ShowWindow(_hwnd, IsIconic(_hwnd) ? SwRestore : SwShow);
        Activate();
        SetForegroundWindow(_hwnd);
    }

    private void OnAppWindowClosing(AppWindow sender, AppWindowClosingEventArgs args)
    {
        if (_allowClose)
            return;
        // Closing the window keeps winhand running in the notification area.
        args.Cancel = true;
        HideSecret();
        ShowWindow(_hwnd, SwHide);
    }

    private IReadOnlyList<NativeTray.MenuItem?> TrayMenu()
    {
        var view = CurrentStatus;
        var state = Json.Str(_status, "state");
        return
        [
            new("打开 winhand", ShowMainWindow),
            null,
            new($"状态：{view.Label}", () => { }, Enabled: false),
            new("重新连接", () => _ = ReconnectAsync()),
            new("断开连接", () => _ = DisconnectAsync(), Enabled: _backend.IsRunning && state is "online" or "connecting" or "offline"),
            null,
            .. (_updater.Ready
                ? new NativeTray.MenuItem?[] { new($"重启并更新到 {_updater.LatestVersion}", () => _ = InstallUpdateAsync()) }
                : _updater.Available
                    ? new NativeTray.MenuItem?[] { new($"查看新版本 {_updater.LatestVersion}…", () => { ShowMainWindow(); ShowPage("settings"); }) }
                    : []),
            new("退出 winhand", () => _ = ExitAsync())
        ];
    }

    private async Task ExitAsync()
    {
        if (_shuttingDown)
            return;
        _shuttingDown = true;
        _ticker.Stop();
        await _backend.StopAsync();
        _tray.Dispose();
        _allowClose = true;
        _updater.ApplyOnExit(); // a downloaded update installs silently once we are gone
        Close();
        Application.Current.Exit();
    }

    // ----------------------------------------------------------------- helpers

    private static T Resource<T>(string key) => (T)Application.Current.Resources[key];

    private static Style PillStyle(Tone tone) => Resource<Style>(tone switch
    {
        Tone.Success => "StatusSuccessStyle",
        Tone.Warning => "StatusWarningStyle",
        Tone.Error => "StatusErrorStyle",
        Tone.Active => "StatusActiveStyle",
        _ => "StatusNeutralStyle"
    });

    private static ContentControl Pill(string text, Tone tone) => new() { Content = text, Style = PillStyle(tone) };

    private static ContentControl Card(UIElement content) => new() { Content = content, Style = Resource<Style>("SurfaceCardStyle") };

    private static TextBlock Text(string text, string style = "BodyCopyStyle") =>
        new() { Text = text, Style = Resource<Style>(style) };

    private static Button ActionButton(string text, Action onClick, bool accent = false)
    {
        var button = new Button { Content = text };
        if (accent)
        {
            button.Style = Resource<Style>("AccentButtonStyle");
            button.MinHeight = 36; // the keyed accent style skips the app-wide 36 px button height
        }
        button.Click += (_, _) => onClick();
        return button;
    }

    private static void OpenPath(string? path, bool select = false)
    {
        if (string.IsNullOrEmpty(path))
            return;
        try
        {
            if (select)
                Process.Start("explorer.exe", $"/select,\"{path}\"");
            else
            {
                if (!Path.HasExtension(path))
                    Directory.CreateDirectory(path);
                Process.Start(new ProcessStartInfo(path) { UseShellExecute = true });
            }
        }
        catch (Exception)
        {
            // opening a folder is best effort
        }
    }

    [DllImport("user32.dll")] private static extern bool ShowWindow(nint hWnd, int command);
    [DllImport("user32.dll")] private static extern uint GetDpiForWindow(nint hWnd);
    [DllImport("user32.dll")] private static extern bool IsIconic(nint hWnd);
    [DllImport("user32.dll")] private static extern bool SetForegroundWindow(nint hWnd);
}
