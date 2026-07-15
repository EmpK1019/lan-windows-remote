using System;
using System.ComponentModel;
using System.Drawing;
using System.Reflection;
using System.Windows.Forms;

internal static class ControlWindowHostTests
{
    [STAThread]
    private static int Main(string[] args)
    {
        if (args.Length != 1)
        {
            Console.Error.WriteLine("Control host assembly path is required.");
            return 2;
        }

        try
        {
            Assembly assembly = Assembly.LoadFrom(args[0]);
            TestUrlValidation(assembly);
            TestFullscreenRestore(assembly, false);
            TestFullscreenRestore(assembly, true);
            Console.WriteLine("CONTROL_HOST_STATE_TESTS_OK");
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine(ex.ToString());
            return 1;
        }
    }

    private static void TestUrlValidation(Assembly assembly)
    {
        Type program = RequiredType(assembly, "WindowsLANRemoteControlHost.Program");
        MethodInfo tryReadUrl = program.GetMethod("TryReadUrl", BindingFlags.NonPublic | BindingFlags.Static);
        if (tryReadUrl == null) throw new InvalidOperationException("TryReadUrl was not found.");

        object[] valid = {
            new[] { "--url", "http://127.0.0.1:8765/?remote=1&handoff=abcdefghijklmnop" },
            null
        };
        if (!(bool)tryReadUrl.Invoke(null, valid)) throw new InvalidOperationException("Valid URL was rejected.");

        object[] invalid = {
            new[] { "--url", "http://127.0.0.1:8765/?notremote=1&handoff=abcdefghijklmnop" },
            null
        };
        if ((bool)tryReadUrl.Invoke(null, invalid)) throw new InvalidOperationException("Invalid URL was accepted.");
    }

    private static void TestFullscreenRestore(Assembly assembly, bool startMaximized)
    {
        Type windowType = RequiredType(assembly, "WindowsLANRemoteControlHost.ControlWindow");
        ConstructorInfo constructor = windowType.GetConstructor(
            BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic,
            null,
            new[] { typeof(Uri) },
            null);
        MethodInfo toggle = windowType.GetMethod("ToggleFullscreen", BindingFlags.Instance | BindingFlags.NonPublic);
        if (constructor == null || toggle == null) throw new InvalidOperationException("Control window members were not found.");

        using (Form window = (Form)constructor.Invoke(new object[] {
            new Uri("http://127.0.0.1:8765/?remote=1&handoff=abcdefghijklmnop")
        }))
        {
            SuppressShownHandler(window);
            window.StartPosition = FormStartPosition.Manual;
            window.ShowInTaskbar = false;
            window.Opacity = 0;
            window.Bounds = new Rectangle(140, 120, 900, 620);
            window.Show();
            Application.DoEvents();
            Rectangle expectedBounds = window.Bounds;
            if (startMaximized)
            {
                window.WindowState = FormWindowState.Maximized;
                Application.DoEvents();
                if (window.WindowState != FormWindowState.Maximized)
                    throw new InvalidOperationException("Test window could not enter maximized state.");
                expectedBounds = window.RestoreBounds;
            }

            toggle.Invoke(window, null);
            if (!window.TopMost) throw new InvalidOperationException("Fullscreen did not enable TopMost.");
            toggle.Invoke(window, null);

            if (window.TopMost) throw new InvalidOperationException("Fullscreen did not restore TopMost.");
            if (startMaximized && window.WindowState != FormWindowState.Maximized)
                throw new InvalidOperationException("Maximized state was not restored.");
            if (!startMaximized && window.WindowState != FormWindowState.Normal)
                throw new InvalidOperationException("Normal state was not restored.");
            if (!startMaximized && window.Bounds != expectedBounds)
                throw new InvalidOperationException("Normal bounds were not restored.");
            window.Hide();
        }
    }

    private static void SuppressShownHandler(Form window)
    {
        FieldInfo shownKey = typeof(Form).GetField("EVENT_SHOWN", BindingFlags.NonPublic | BindingFlags.Static);
        PropertyInfo eventsProperty = typeof(Component).GetProperty("Events", BindingFlags.NonPublic | BindingFlags.Instance);
        if (shownKey == null || eventsProperty == null)
            throw new InvalidOperationException("WinForms Shown event metadata was not found.");
        EventHandlerList events = (EventHandlerList)eventsProperty.GetValue(window, null);
        object key = shownKey.GetValue(null);
        Delegate handler = events[key];
        if (handler != null) events.RemoveHandler(key, handler);
    }

    private static Type RequiredType(Assembly assembly, string name)
    {
        Type result = assembly.GetType(name, false);
        if (result == null) throw new InvalidOperationException(name + " was not found.");
        return result;
    }
}
