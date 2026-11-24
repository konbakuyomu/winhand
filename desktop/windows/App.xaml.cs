using Microsoft.UI.Xaml;

namespace Winhand.Desktop;

public partial class App : Application
{
    private static MainWindow? _window;

    public App()
    {
        // Development previews only: the theme to take screenshots in. It is set on the application, not
        // on the window, so that brushes the pages take from Application.Resources follow it as well.
        if (!string.IsNullOrEmpty(Environment.GetEnvironmentVariable("WINHAND_DESKTOP_INSTANCE")))
        {
            if (Environment.GetEnvironmentVariable("WINHAND_DESKTOP_THEME") == "light")
                RequestedTheme = ApplicationTheme.Light;
            else if (Environment.GetEnvironmentVariable("WINHAND_DESKTOP_THEME") == "dark")
                RequestedTheme = ApplicationTheme.Dark;
        }
        InitializeComponent();
        CrashGuard.Install();
        // An exception from an event handler or a layout pass ends the app unless it is marked
        // handled here (WinUI fails fast otherwise); with the app went the backend and the relay.
        UnhandledException += (_, e) =>
        {
            CrashGuard.Write("ui", e.Exception, e.Message);
            if (_window is null)
                return; // failing to start: an app without its window would only hold the single-instance key
            e.Handled = true;
            _window.ShowUiErrorNotice();
        };
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        _window ??= new MainWindow();
        if (!Program.StartInBackground)
            _window.ShowMainWindow();
    }

    internal static void ShowFromRedirect()
    {
        if (_window is { } window)
            window.DispatcherQueue.TryEnqueue(window.ShowMainWindow);
    }
}
