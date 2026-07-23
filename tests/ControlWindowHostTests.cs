using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Drawing;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Threading;
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
            TestRemoteWindowStartup(assembly);
            TestFullscreenRestore(assembly, false);
            TestFullscreenRestore(assembly, true);
            TestCloseToTray(assembly);
            TestKeyboardCaptureSurface(assembly);
            TestMouseCaptureMappings(assembly);
            TestFillModeMouseMapping(assembly);
            TestNativeGlassToolbar(assembly);
            TestNativeInputTransport(assembly);
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

        object[] validMain = {
            new[] { "--url", "http://127.0.0.1:8765/?v=0.6.6&maximized=1" },
            null
        };
        if (!(bool)tryReadUrl.Invoke(null, validMain)) throw new InvalidOperationException("Valid main-window URL was rejected.");

        object[] invalid = {
            new[] { "--url", "http://127.0.0.1:8765/?notremote=1&handoff=abcdefghijklmnop" },
            null
        };
        if ((bool)tryReadUrl.Invoke(null, invalid)) throw new InvalidOperationException("Invalid URL was accepted.");

        object[] missingHandoff = {
            new[] { "--url", "http://127.0.0.1:8765/?remote=1" },
            null
        };
        if ((bool)tryReadUrl.Invoke(null, missingHandoff))
            throw new InvalidOperationException("Remote URL without handoff was accepted.");
    }

    private static void TestRemoteWindowStartup(Assembly assembly)
    {
        Type windowType = RequiredType(assembly, "WindowsLANRemoteControlHost.ControlWindow");
        ConstructorInfo constructor = windowType.GetConstructor(
            BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic,
            null,
            new[] { typeof(Uri) },
            null);
        MethodInfo dataFolder = windowType.GetMethod("BrowserDataFolder", BindingFlags.Instance | BindingFlags.NonPublic);
        MethodInfo activate = windowType.GetMethod("ActivateRemoteWindow", BindingFlags.Instance | BindingFlags.NonPublic);
        MethodInfo promote = windowType.GetMethod("PromoteInitialRemoteWindow", BindingFlags.Instance | BindingFlags.NonPublic);
        if (constructor == null || dataFolder == null || activate == null || promote == null)
            throw new InvalidOperationException("Remote startup members were not found.");

        using (Form mainWindow = (Form)constructor.Invoke(new object[] {
            new Uri("http://127.0.0.1:8765/?v=1.2.1")
        }))
        using (Form remoteWindow = (Form)constructor.Invoke(new object[] {
            new Uri("http://127.0.0.1:8765/?remote=1&handoff=abcdefghijklmnop")
        }))
        {
            string mainFolder = Convert.ToString(dataFolder.Invoke(mainWindow, null));
            string remoteFolder = Convert.ToString(dataFolder.Invoke(remoteWindow, null));
            if (String.Equals(mainFolder, remoteFolder, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("Main and remote WebView2 windows share one user data folder.");

            object taskValue = activate.Invoke(mainWindow, new object[] { 0 });
            System.Threading.Tasks.Task<bool> task = taskValue as System.Threading.Tasks.Task<bool>;
            if (task == null || task.GetAwaiter().GetResult())
                throw new InvalidOperationException("Invalid remote process activation was accepted.");
        }
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

    private static void TestCloseToTray(Assembly assembly)
    {
        Type windowType = RequiredType(assembly, "WindowsLANRemoteControlHost.ControlWindow");
        ConstructorInfo constructor = windowType.GetConstructor(
            BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic,
            null,
            new[] { typeof(Uri) },
            null);
        MethodInfo setCloseToTray = windowType.GetMethod("SetCloseToTray", BindingFlags.Instance | BindingFlags.NonPublic);
        MethodInfo restoreFromTray = windowType.GetMethod("RestoreFromTray", BindingFlags.Instance | BindingFlags.NonPublic);
        MethodInfo exitFromTray = windowType.GetMethod("ExitFromTray", BindingFlags.Instance | BindingFlags.NonPublic);
        if (constructor == null || setCloseToTray == null || restoreFromTray == null || exitFromTray == null)
            throw new InvalidOperationException("Tray window members were not found.");

        using (Form mainWindow = (Form)constructor.Invoke(new object[] {
            new Uri("http://127.0.0.1:8765/?v=0.6.16")
        }))
        {
            SuppressShownHandler(mainWindow);
            mainWindow.ShowInTaskbar = false;
            mainWindow.Opacity = 0;
            mainWindow.Show();
            Application.DoEvents();
            setCloseToTray.Invoke(mainWindow, new object[] { true });
            mainWindow.Close();
            Application.DoEvents();
            if (mainWindow.IsDisposed || mainWindow.Visible)
                throw new InvalidOperationException("Closing the main window did not hide it to the tray.");

            restoreFromTray.Invoke(mainWindow, null);
            Application.DoEvents();
            if (!mainWindow.Visible || mainWindow.IsDisposed)
                throw new InvalidOperationException("The main window could not be restored from the tray.");

            exitFromTray.Invoke(mainWindow, null);
            Application.DoEvents();
            if (!mainWindow.IsDisposed)
                throw new InvalidOperationException("Tray Exit did not close the main window.");
        }

        using (Form remoteWindow = (Form)constructor.Invoke(new object[] {
            new Uri("http://127.0.0.1:8765/?remote=1&handoff=abcdefghijklmnop")
        }))
        {
            SuppressShownHandler(remoteWindow);
            remoteWindow.ShowInTaskbar = false;
            remoteWindow.Opacity = 0;
            remoteWindow.Show();
            Application.DoEvents();
            setCloseToTray.Invoke(remoteWindow, new object[] { true });
            remoteWindow.Close();
            Application.DoEvents();
            if (!remoteWindow.IsDisposed)
                throw new InvalidOperationException("A remote control window was incorrectly hidden to the tray.");
        }
    }

    private static void TestKeyboardCaptureSurface(Assembly assembly)
    {
        Type windowType = RequiredType(assembly, "WindowsLANRemoteControlHost.ControlWindow");
        ConstructorInfo constructor = windowType.GetConstructor(
            BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic,
            null,
            new[] { typeof(Uri) },
            null);
        MethodInfo initializeHook = windowType.GetMethod("InitializeKeyboardHook", BindingFlags.Instance | BindingFlags.NonPublic);
        MethodInfo setCapture = windowType.GetMethod("SetKeyboardCapture", BindingFlags.Instance | BindingFlags.NonPublic);
        MethodInfo hookCallback = windowType.GetMethod("KeyboardHookCallback", BindingFlags.Instance | BindingFlags.NonPublic);
        MethodInfo forwardKey = windowType.GetMethod("ForwardNativeKey", BindingFlags.Instance | BindingFlags.NonPublic);
        MethodInfo initializeMouseHook = windowType.GetMethod("InitializeMouseHook", BindingFlags.Instance | BindingFlags.NonPublic);
        MethodInfo configureNativeInput = windowType.GetMethod("ConfigureNativeInput", BindingFlags.Instance | BindingFlags.NonPublic);
        MethodInfo nativeInputLoop = windowType.GetMethod("NativeInputLoop", BindingFlags.Instance | BindingFlags.NonPublic);
        if (constructor == null || initializeHook == null || setCapture == null || hookCallback == null ||
            forwardKey == null || initializeMouseHook == null || configureNativeInput == null || nativeInputLoop == null)
            throw new InvalidOperationException("Native input capture members were not found.");

        using (Form remoteWindow = (Form)constructor.Invoke(new object[] {
            new Uri("http://127.0.0.1:8765/?remote=1&handoff=abcdefghijklmnop")
        }))
        {
            SuppressShownHandler(remoteWindow);
            Dictionary<string, object> mouseFallbackConfiguration = new Dictionary<string, object>
            {
                { "enabled", true },
                { "endpoint", "http://127.0.0.1:8765/input" },
                { "token", "abcdefghijklmnop" },
                { "monitor", "all" },
                { "remote_width", 1920 },
                { "remote_height", 1080 },
                { "content_left", 0.0 },
                { "content_top", 0.0 },
                { "content_width", 800.0 },
                { "content_height", 600.0 },
                { "keyboard_enabled", false },
                { "suspended", false }
            };
            if ((bool)configureNativeInput.Invoke(remoteWindow, new object[] { mouseFallbackConfiguration }))
                throw new InvalidOperationException("Native mouse input was reported active without an installed hook.");
            bool enabledWithoutHook = (bool)setCapture.Invoke(remoteWindow, new object[] { true });
            if (enabledWithoutHook)
                throw new InvalidOperationException("Keyboard capture enabled without an installed hook.");
        }
    }

    private static void TestMouseCaptureMappings(Assembly assembly)
    {
        Type windowType = RequiredType(assembly, "WindowsLANRemoteControlHost.ControlWindow");
        MethodInfo tryMouseButton = windowType.GetMethod(
            "TryMouseButton",
            BindingFlags.Static | BindingFlags.NonPublic);
        MethodInfo shouldPassInjectedMouseInput = windowType.GetMethod(
            "ShouldPassInjectedMouseInput",
            BindingFlags.Static | BindingFlags.NonPublic);
        FieldInfo horizontalWheel = windowType.GetField(
            "WmMouseHWheel",
            BindingFlags.Static | BindingFlags.NonPublic);
        FieldInfo remoteInputExtraInfo = windowType.GetField(
            "RemoteInputExtraInfo",
            BindingFlags.Static | BindingFlags.NonPublic);
        if (tryMouseButton == null || shouldPassInjectedMouseInput == null ||
            horizontalWheel == null || remoteInputExtraInfo == null)
            throw new InvalidOperationException("Extended mouse capture members were not found.");
        if ((int)horizontalWheel.GetRawConstantValue() != 0x020E)
            throw new InvalidOperationException("Horizontal wheel message is incorrect.");
        if ((ulong)remoteInputExtraInfo.GetRawConstantValue() != 0x4C414E52UL)
            throw new InvalidOperationException("Remote input marker is incorrect.");

        AssertMouseButton(tryMouseButton, 0x020B, 1U << 16, 3, true);
        AssertMouseButton(tryMouseButton, 0x020C, 1U << 16, 3, false);
        AssertMouseButton(tryMouseButton, 0x020B, 2U << 16, 4, true);
        AssertMouseButton(tryMouseButton, 0x020C, 2U << 16, 4, false);
        if (!(bool)shouldPassInjectedMouseInput.Invoke(
            null,
            new object[] { 1U, new UIntPtr(0x4C414E52UL) }))
            throw new InvalidOperationException("LAN Remote's own injected mouse input was recaptured.");
        if ((bool)shouldPassInjectedMouseInput.Invoke(
            null,
            new object[] { 1U, new UIntPtr(0x1234UL) }))
            throw new InvalidOperationException("Touchpad or vendor-injected mouse input bypassed native capture.");
        if ((bool)shouldPassInjectedMouseInput.Invoke(
            null,
            new object[] { 0U, UIntPtr.Zero }))
            throw new InvalidOperationException("Physical mouse input bypassed native capture.");
    }

    private static void TestFillModeMouseMapping(Assembly assembly)
    {
        Type windowType = RequiredType(assembly, "WindowsLANRemoteControlHost.ControlWindow");
        MethodInfo applyCorners = windowType.GetMethod(
            "ApplyWindowCornerPreference",
            BindingFlags.Instance | BindingFlags.NonPublic);
        if (applyCorners == null)
            throw new InvalidOperationException("Main and remote windows do not expose the DWM corner preference.");
        Type sessionType = windowType.GetNestedType("NativeInputSession", BindingFlags.NonPublic);
        MethodInfo mapRemotePoint = windowType.GetMethod(
            "MapRemotePoint",
            BindingFlags.Static | BindingFlags.NonPublic);
        if (sessionType == null || mapRemotePoint == null)
            throw new InvalidOperationException("Fill-mode mouse mapping members were not found.");

        object session = Activator.CreateInstance(sessionType, true);
        sessionType.GetField("RemoteBounds").SetValue(session, new RectangleF(0, 0, 800, 600));
        sessionType.GetField("RemoteWidth").SetValue(session, 1920);
        sessionType.GetField("RemoteHeight").SetValue(session, 1080);
        sessionType.GetField("FillMode").SetValue(session, true);

        object[] leftEdge = { session, 0, 300, 0, 0 };
        mapRemotePoint.Invoke(null, leftEdge);
        if (Math.Abs((int)leftEdge[3]) > 1 || Math.Abs((int)leftEdge[4] - 540) > 1)
            throw new InvalidOperationException("Adaptive fill-mode did not map the full left edge.");

        object[] center = { session, 400, 300, 0, 0 };
        mapRemotePoint.Invoke(null, center);
        if (Math.Abs((int)center[3] - 960) > 1 || Math.Abs((int)center[4] - 540) > 1)
            throw new InvalidOperationException("Fill-mode center did not map to the remote center.");
    }

    private static void TestNativeGlassToolbar(Assembly assembly)
    {
        Type toolbarType = RequiredType(assembly, "WindowsLANRemoteControlHost.NativeGlassToolbar");
        ConstructorInfo constructor = toolbarType.GetConstructor(new[] { typeof(Action<string, string>) });
        MethodInfo updateState = toolbarType.GetMethod("UpdateState");
        MethodInfo positionForOwner = toolbarType.GetMethod("PositionForOwner");
        MethodInfo applyUserLocation = toolbarType.GetMethod(
            "ApplyUserLocation",
            BindingFlags.Instance | BindingFlags.NonPublic);
        FieldInfo fpsField = toolbarType.GetField("fps", BindingFlags.Instance | BindingFlags.NonPublic);
        FieldInfo remoteLockField = toolbarType.GetField("remoteLock", BindingFlags.Instance | BindingFlags.NonPublic);
        if (constructor == null || updateState == null || positionForOwner == null ||
            applyUserLocation == null || fpsField == null || remoteLockField == null)
            throw new InvalidOperationException("Native glass toolbar members were not found.");
        if (toolbarType.GetField("scale", BindingFlags.Instance | BindingFlags.NonPublic) != null)
            throw new InvalidOperationException("Removed scale-mode control is still present in the native toolbar.");

        string observedAction = String.Empty;
        using (Form toolbar = (Form)constructor.Invoke(new object[] {
            new Action<string, string>(delegate(string action, string value) { observedAction = action + value; })
        }))
        {
            Dictionary<string, object> expanded = new Dictionary<string, object>
            {
                { "collapsed", false },
                { "view_only", false },
                { "fps", 120 },
                { "scale_mode", "fill" },
                { "keyboard", true },
                { "clipboard", true },
                { "unlock_visible", true },
                { "fullscreen", false },
                { "status_error", false },
                { "monitors", new object[] { new Dictionary<string, object> { { "id", "all" }, { "label", "全部显示器" } } } }
            };
            updateState.Invoke(toolbar, new object[] { expanded });
            if (toolbar.FormBorderStyle != FormBorderStyle.None || toolbar.Opacity > 0.42)
                throw new InvalidOperationException("Native toolbar is not using the translucent borderless glass shell.");
            object fpsButton = fpsField.GetValue(toolbar);
            string fpsCaption = Convert.ToString(fpsButton.GetType().GetProperty("Caption").GetValue(fpsButton, null));
            Font fpsFont = (Font)fpsButton.GetType().GetProperty("Font").GetValue(fpsButton, null);
            if (!String.Equals(fpsCaption, "120 FPS", StringComparison.Ordinal) ||
                !fpsFont.Name.Equals("Noto Sans SC", StringComparison.OrdinalIgnoreCase) ||
                fpsFont.Size < 11.1f ||
                fpsFont.Bold ||
                ((Control)fpsButton).Width != 58 ||
                toolbar.Height != 43 ||
                toolbar.Width < 260)
                throw new InvalidOperationException(
                    "Native toolbar did not mirror the Web toolbar controls: fps=" +
                    fpsCaption + ", width=" + toolbar.Width.ToString());
            object remoteLockButton = remoteLockField.GetValue(toolbar);
            string remoteLockIcon = Convert.ToString(
                remoteLockButton.GetType().GetProperty("IconKind").GetValue(remoteLockButton, null));
            bool remoteLockActive = Convert.ToBoolean(
                remoteLockButton.GetType().GetProperty("Active").GetValue(remoteLockButton, null));
            if (!String.Equals(remoteLockIcon, "Lock", StringComparison.Ordinal) || !remoteLockActive)
                throw new InvalidOperationException("Locked state did not switch the native toolbar to its active closed-lock icon.");

            Rectangle ownerBounds = new Rectangle(100, 200, 1400, 900);
            applyUserLocation.Invoke(toolbar, new object[] { ownerBounds, new Point(420, 360) });
            Point floatingLocation = toolbar.Location;
            if (floatingLocation.Y != 360 || !String.Equals(observedAction, "toolbar_docked0", StringComparison.Ordinal))
                throw new InvalidOperationException("Floating toolbar did not preserve its freely dragged location.");
            positionForOwner.Invoke(toolbar, new object[] { ownerBounds, new Point(600, 208) });
            if (toolbar.Location != floatingLocation)
                throw new InvalidOperationException("Owner layout unexpectedly reset the user-positioned toolbar.");

            applyUserLocation.Invoke(toolbar, new object[] { ownerBounds, new Point(500, 207) });
            if (toolbar.Top != ownerBounds.Top + 8 || !String.Equals(observedAction, "toolbar_docked1", StringComparison.Ordinal))
                throw new InvalidOperationException("Toolbar did not snap to the true top-edge target and arm auto-hide.");

            expanded["collapsed"] = true;
            updateState.Invoke(toolbar, new object[] { expanded });
            if (toolbar.Width > 60 || toolbar.Height > 36)
                throw new InvalidOperationException("Collapsed native toolbar did not become a compact glass handle.");
            positionForOwner.Invoke(toolbar, new object[] { ownerBounds, new Point(600, 208) });
            if (toolbar.Top != ownerBounds.Top)
                throw new InvalidOperationException("Collapsed native toolbar did not attach to the owner's top edge.");
        }
        GC.KeepAlive(observedAction);
    }

    private static void AssertMouseButton(
        MethodInfo tryMouseButton,
        int message,
        uint mouseData,
        int expectedButton,
        bool expectedDown)
    {
        object[] arguments = { message, mouseData, 0, false };
        if (!(bool)tryMouseButton.Invoke(null, arguments))
            throw new InvalidOperationException("Mouse side button message was rejected.");
        if ((int)arguments[2] != expectedButton || (bool)arguments[3] != expectedDown)
            throw new InvalidOperationException("Mouse side button mapping is incorrect.");
    }

    private static void TestNativeInputTransport(Assembly assembly)
    {
        Type windowType = RequiredType(assembly, "WindowsLANRemoteControlHost.ControlWindow");
        Type sessionType = windowType.GetNestedType("NativeInputSession", BindingFlags.NonPublic);
        Type transportType = windowType.GetNestedType("NativeInputTransport", BindingFlags.NonPublic);
        if (sessionType == null || transportType == null)
            throw new InvalidOperationException("Native input transport types were not found.");

        TcpListener listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        int port = ((IPEndPoint)listener.LocalEndpoint).Port;
        string received = null;
        Exception serverFailure = null;
        Thread server = new Thread(new ThreadStart(delegate
        {
            try
            {
                using (TcpClient client = listener.AcceptTcpClient())
                using (NetworkStream stream = client.GetStream())
                {
                    stream.ReadTimeout = 3000;
                    string header = ReadHeader(stream);
                    if (!header.StartsWith("CONNECT /input-stream HTTP/1.1", StringComparison.Ordinal))
                        throw new InvalidOperationException("Native input CONNECT handshake was not received.");
                    byte[] response = Encoding.ASCII.GetBytes(
                        "HTTP/1.1 200 Connection Established\r\nX-LAN-Input-Protocol: 1\r\n\r\n");
                    stream.Write(response, 0, response.Length);
                    byte[] lengthBytes = ReadExact(stream, 4);
                    int length =
                        (lengthBytes[0] << 24) |
                        (lengthBytes[1] << 16) |
                        (lengthBytes[2] << 8) |
                        lengthBytes[3];
                    received = Encoding.UTF8.GetString(ReadExact(stream, length));
                    stream.WriteByte(0);
                    stream.Flush();
                }
            }
            catch (Exception ex)
            {
                serverFailure = ex;
            }
        }));
        server.IsBackground = true;
        server.Start();

        object session = Activator.CreateInstance(sessionType, true);
        sessionType.GetField("Endpoint").SetValue(session, new Uri("http://127.0.0.1:" + port + "/input"));
        sessionType.GetField("Token").SetValue(session, "abcdefghijklmnop");
        object transport = Activator.CreateInstance(transportType, true);
        try
        {
            MethodInfo send = transportType.GetMethod("Send");
            if (send == null) throw new InvalidOperationException("Native input Send method was not found.");
            bool sent = (bool)send.Invoke(
                transport,
                new object[] { session, "{\"type\":\"native_key_down\",\"scan_code\":59,\"extended\":false}" });
            if (!sent) throw new InvalidOperationException("Native input transport rejected a valid frame.");
        }
        finally
        {
            IDisposable disposable = transport as IDisposable;
            if (disposable != null) disposable.Dispose();
            listener.Stop();
        }
        if (!server.Join(4000)) throw new InvalidOperationException("Native input test server did not stop.");
        if (serverFailure != null) throw new InvalidOperationException("Native input test server failed.", serverFailure);
        if (received == null || received.IndexOf("\"scan_code\":59", StringComparison.Ordinal) < 0)
            throw new InvalidOperationException("Native input frame payload was not received.");
    }

    private static string ReadHeader(NetworkStream stream)
    {
        MemoryStream buffer = new MemoryStream();
        int matched = 0;
        while (buffer.Length < 8192)
        {
            int value = stream.ReadByte();
            if (value < 0) throw new EndOfStreamException();
            buffer.WriteByte((byte)value);
            if ((matched == 0 || matched == 2) && value == '\r') matched++;
            else if ((matched == 1 || matched == 3) && value == '\n')
            {
                matched++;
                if (matched == 4) return Encoding.ASCII.GetString(buffer.ToArray());
            }
            else matched = value == '\r' ? 1 : 0;
        }
        throw new InvalidOperationException("Native input request header was too large.");
    }

    private static byte[] ReadExact(NetworkStream stream, int length)
    {
        byte[] result = new byte[length];
        int offset = 0;
        while (offset < length)
        {
            int read = stream.Read(result, offset, length - offset);
            if (read <= 0) throw new EndOfStreamException();
            offset += read;
        }
        return result;
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
