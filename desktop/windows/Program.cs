using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;
using Microsoft.Windows.AppLifecycle;

namespace Winhand.Desktop;

public static class Program
{
    private const string InstanceKey = "Winhand.Desktop";

    /// <summary>Started by the logon Run entry: stay in the notification area, no window.</summary>
    internal static bool StartInBackground { get; private set; }

    [STAThread]
    public static void Main(string[] args)
    {
        // Installer/updater hooks must run and exit before anything else starts.
        Velopack.VelopackApp.Build().SetAutoApplyOnStartup(false).Run();
        WinRT.ComWrappersSupport.InitializeComWrappers();
        StartInBackground = args.Contains(Autostart.BackgroundArgument);
        var current = AppInstance.GetCurrent();
        var main = AppInstance.FindOrRegisterForKey(InstanceKey);
        if (!main.IsCurrent)
        {
            // A second launch (Start menu, double click) just brings the running app forward.
            Task.Run(async () => await main.RedirectActivationToAsync(current.GetActivatedEventArgs()))
                .GetAwaiter().GetResult();
            return;
        }

        main.Activated += (_, _) => App.ShowFromRedirect();
        Application.Start(_ =>
        {
            SynchronizationContext.SetSynchronizationContext(
                new DispatcherQueueSynchronizationContext(DispatcherQueue.GetForCurrentThread()));
            new App();
        });
    }
}
