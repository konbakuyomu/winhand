using Microsoft.UI.Xaml;

namespace Winhand.Desktop;

public partial class App : Application
{
    private static MainWindow? _window;

    public App()
    {
        InitializeComponent();
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
