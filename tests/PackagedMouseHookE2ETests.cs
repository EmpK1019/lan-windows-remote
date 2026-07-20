using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Drawing;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;

internal static class PackagedMouseHookE2ETests
{
    private const uint MouseEventLeftDown = 0x0002;
    private const uint MouseEventLeftUp = 0x0004;
    private const uint MouseEventWheel = 0x0800;
    private const int WhMouseLl = 14;

    [StructLayout(LayoutKind.Sequential)]
    private struct NativePoint
    {
        public int X;
        public int Y;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct LowLevelMouseInput
    {
        public NativePoint Point;
        public uint MouseData;
        public uint Flags;
        public uint Time;
        public UIntPtr ExtraInfo;
    }

    private delegate IntPtr LowLevelMouseProc(int code, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern bool SetCursorPos(int x, int y);

    [DllImport("user32.dll")]
    private static extern bool GetCursorPos(out NativePoint point);

    [DllImport("user32.dll")]
    private static extern void mouse_event(uint flags, uint dx, uint dy, int data, UIntPtr extraInfo);

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr window);

    [DllImport("user32.dll")]
    private static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr window, IntPtr processId);

    [DllImport("kernel32.dll")]
    private static extern uint GetCurrentThreadId();

    [DllImport("user32.dll")]
    private static extern bool AttachThreadInput(uint attach, uint attachTo, bool value);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr SetWindowsHookEx(
        int hookId,
        LowLevelMouseProc callback,
        IntPtr module,
        uint threadId);

    [DllImport("user32.dll")]
    private static extern IntPtr CallNextHookEx(
        IntPtr hook,
        int code,
        IntPtr wParam,
        IntPtr lParam);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool UnhookWindowsHookEx(IntPtr hook);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr GetModuleHandle(string moduleName);

    [STAThread]
    private static int Main(string[] args)
    {
        if (args.Length != 1)
        {
            Console.Error.WriteLine("Usage: PackagedMouseHookE2ETests.exe <ControlWindowHost.exe>");
            return 2;
        }

        TcpListener listener = new TcpListener(IPAddress.Loopback, 0);
        List<string> payloads = new List<string>();
        Exception serverFailure = null;
        ManualResetEvent receivedInput = new ManualResetEvent(false);
        listener.Start();
        int port = ((IPEndPoint)listener.LocalEndpoint).Port;

        Thread server = new Thread(new ThreadStart(delegate
        {
            try
            {
                using (TcpClient client = listener.AcceptTcpClient())
                using (NetworkStream stream = client.GetStream())
                {
                    stream.ReadTimeout = 5000;
                    string header = ReadHeader(stream);
                    if (!header.StartsWith("CONNECT /input-stream HTTP/1.1", StringComparison.Ordinal))
                        throw new InvalidDataException("Native input CONNECT request was not received.");
                    byte[] response = Encoding.ASCII.GetBytes(
                        "HTTP/1.1 200 Connection Established\r\n" +
                        "X-LAN-Input-Protocol: 1\r\n\r\n");
                    stream.Write(response, 0, response.Length);
                    stream.Flush();

                    while (true)
                    {
                        byte[] lengthBytes = ReadExact(stream, 4);
                        int length =
                            (lengthBytes[0] << 24) |
                            (lengthBytes[1] << 16) |
                            (lengthBytes[2] << 8) |
                            lengthBytes[3];
                        if (length < 2 || length > 4096)
                            throw new InvalidDataException("Invalid native input frame length.");
                        string payload = Encoding.UTF8.GetString(ReadExact(stream, length));
                        lock (payloads)
                        {
                            payloads.Add(payload);
                            if (
                                ContainsType(payloads, "mouse_down") &&
                                ContainsType(payloads, "mouse_up") &&
                                ContainsType(payloads, "mouse_wheel"))
                            {
                                receivedInput.Set();
                            }
                        }
                        stream.WriteByte(0);
                        stream.Flush();
                        if (receivedInput.WaitOne(0))
                            return;
                    }
                }
            }
            catch (Exception ex)
            {
                serverFailure = ex;
                receivedInput.Set();
            }
        }));
        server.IsBackground = true;
        server.Start();

        try
        {
            Assembly assembly = Assembly.LoadFrom(Path.GetFullPath(args[0]));
            Type windowType = assembly.GetType("WindowsLANRemoteControlHost.ControlWindow", true);
            ConstructorInfo constructor = windowType.GetConstructor(
                BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic,
                null,
                new[] { typeof(Uri) },
                null);
            MethodInfo initializeMouseHook = windowType.GetMethod(
                "InitializeMouseHook",
                BindingFlags.Instance | BindingFlags.NonPublic);
            MethodInfo startNativeInputWorker = windowType.GetMethod(
                "StartNativeInputWorker",
                BindingFlags.Instance | BindingFlags.NonPublic);
            MethodInfo configureNativeInput = windowType.GetMethod(
                "ConfigureNativeInput",
                BindingFlags.Instance | BindingFlags.NonPublic);
            if (
                constructor == null ||
                initializeMouseHook == null ||
                startNativeInputWorker == null ||
                configureNativeInput == null)
            {
                throw new InvalidOperationException("Native mouse capture members were not found.");
            }

            using (Form window = (Form)constructor.Invoke(new object[]
            {
                new Uri("http://127.0.0.1:8765/?remote=1&handoff=abcdefghijklmnop")
            }))
            {
                SuppressShownHandler(window);
                window.Text = "LAN Remote native mouse hook audit";
                window.StartPosition = FormStartPosition.CenterScreen;
                window.ClientSize = new Size(900, 620);
                window.TopMost = true;
                Exception sessionFailure = null;
                string summary = String.Empty;
                window.Shown += delegate
                {
                    NativePoint originalCursor;
                    GetCursorPos(out originalCursor);
                    List<string> observedHookEvents = new List<string>();
                    IntPtr auditHook = IntPtr.Zero;
                    LowLevelMouseProc auditHookProc = null;
                    try
                    {
                        initializeMouseHook.Invoke(window, null);
                        startNativeInputWorker.Invoke(window, null);
                        Dictionary<string, object> configuration = new Dictionary<string, object>
                        {
                            { "enabled", true },
                            { "endpoint", "http://127.0.0.1:" + port + "/input" },
                            { "token", "abcdefghijklmnop" },
                            { "monitor", "all" },
                            { "remote_width", 1920 },
                            { "remote_height", 1080 },
                            { "content_left", 0.0 },
                            { "content_top", 0.0 },
                            { "content_width", (double)window.ClientSize.Width },
                            { "content_height", (double)window.ClientSize.Height },
                            { "keyboard_enabled", false },
                            { "suspended", false }
                        };
                        if (!(bool)configureNativeInput.Invoke(window, new object[] { configuration }))
                            throw new InvalidOperationException("Native mouse hook did not become active.");

                        ForceForeground(window);
                        if (GetForegroundWindow() != window.Handle)
                            throw new InvalidOperationException("The native control window could not become foreground.");
                        Point target = window.PointToScreen(
                            new Point(window.ClientSize.Width / 2, window.ClientSize.Height / 2));
                        auditHookProc = delegate(int code, IntPtr message, IntPtr data)
                        {
                            if (code >= 0)
                            {
                                LowLevelMouseInput input = (LowLevelMouseInput)Marshal.PtrToStructure(
                                    data,
                                    typeof(LowLevelMouseInput));
                                observedHookEvents.Add(String.Format(
                                    "msg=0x{0:X} point={1},{2} data=0x{3:X} flags=0x{4:X} extra=0x{5:X}",
                                    message.ToInt32(),
                                    input.Point.X,
                                    input.Point.Y,
                                    input.MouseData,
                                    input.Flags,
                                    input.ExtraInfo.ToUInt64()));
                            }
                            return CallNextHookEx(auditHook, code, message, data);
                        };
                        auditHook = SetWindowsHookEx(
                            WhMouseLl,
                            auditHookProc,
                            GetModuleHandle(null),
                            0);
                        if (auditHook == IntPtr.Zero)
                            throw new InvalidOperationException("Could not install the audit mouse hook.");
                        if (!SetCursorPos(target.X, target.Y))
                            throw new InvalidOperationException("Could not position the audit cursor.");
                        PumpMessages(150);

                        // mouse_event deliberately produces LLMHF_INJECTED input with
                        // dwExtraInfo=0, matching precision touchpad/vendor-generated input.
                        mouse_event(MouseEventLeftDown, 0, 0, 0, UIntPtr.Zero);
                        mouse_event(MouseEventLeftUp, 0, 0, 0, UIntPtr.Zero);
                        mouse_event(MouseEventWheel, 0, 0, 120, UIntPtr.Zero);

                        DateTime deadline = DateTime.UtcNow.AddSeconds(5);
                        while (!receivedInput.WaitOne(0) && DateTime.UtcNow < deadline)
                        {
                            Application.DoEvents();
                            Thread.Sleep(10);
                        }
                        Console.WriteLine(NativeDiagnostics(windowType, window));
                        lock (payloads)
                        {
                            summary = NonMovePayloadSummary(payloads);
                            Console.WriteLine("frames=" + summary);
                        }
                        Console.WriteLine("rawEventCount=" + observedHookEvents.Count);
                        if (!receivedInput.WaitOne(0))
                            throw new TimeoutException("Native mouse hook did not forward click and wheel input.");
                        if (serverFailure != null)
                            throw new InvalidOperationException("Native input audit server failed.", serverFailure);
                        string rawEvents = String.Join(" | ", observedHookEvents.ToArray());
                        if (
                            rawEvents.IndexOf("msg=0x201", StringComparison.Ordinal) < 0 ||
                            rawEvents.IndexOf("msg=0x202", StringComparison.Ordinal) < 0 ||
                            rawEvents.IndexOf("msg=0x20A", StringComparison.Ordinal) < 0)
                        {
                            throw new InvalidOperationException(
                                "Windows did not deliver the injected click and wheel sequence: " + rawEvents);
                        }
                        lock (payloads)
                        {
                            if (
                                !ContainsType(payloads, "mouse_down") ||
                                !ContainsType(payloads, "mouse_up") ||
                                !ContainsType(payloads, "mouse_wheel"))
                            {
                                throw new InvalidOperationException(
                                    "Native input frames were incomplete: " + summary);
                            }
                        }
                    }
                    catch (Exception ex)
                    {
                        sessionFailure = ex;
                    }
                    finally
                    {
                        if (auditHook != IntPtr.Zero)
                            UnhookWindowsHookEx(auditHook);
                        SetCursorPos(originalCursor.X, originalCursor.Y);
                        window.Close();
                    }
                };
                Application.Run(window);
                if (sessionFailure != null)
                    throw sessionFailure;
                Console.WriteLine("PACKAGED_MOUSE_HOOK_E2E_OK " + summary);
            }
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine(ex.ToString());
            return 1;
        }
        finally
        {
            listener.Stop();
            receivedInput.Dispose();
        }
    }

    private static bool ContainsType(List<string> payloads, string type)
    {
        string marker = "\"type\":\"" + type + "\"";
        foreach (string payload in payloads)
        {
            if (payload.IndexOf(marker, StringComparison.Ordinal) >= 0)
                return true;
        }
        return false;
    }

    private static string NonMovePayloadSummary(List<string> payloads)
    {
        List<string> result = new List<string>();
        foreach (string payload in payloads)
        {
            if (payload.IndexOf("\"type\":\"mouse_move\"", StringComparison.Ordinal) < 0)
                result.Add(payload);
        }
        return String.Join(" | ", result.ToArray());
    }

    private static void PumpMessages(int milliseconds)
    {
        DateTime deadline = DateTime.UtcNow.AddMilliseconds(milliseconds);
        while (DateTime.UtcNow < deadline)
        {
            Application.DoEvents();
            Thread.Sleep(5);
        }
    }

    private static void ForceForeground(Form window)
    {
        IntPtr foreground = GetForegroundWindow();
        uint currentThread = GetCurrentThreadId();
        uint foregroundThread = GetWindowThreadProcessId(foreground, IntPtr.Zero);
        bool attached =
            foregroundThread != 0 &&
            foregroundThread != currentThread &&
            AttachThreadInput(currentThread, foregroundThread, true);
        try
        {
            window.BringToFront();
            window.Activate();
            SetForegroundWindow(window.Handle);
        }
        finally
        {
            if (attached)
                AttachThreadInput(currentThread, foregroundThread, false);
        }
    }

    private static string NativeDiagnostics(Type windowType, Form window)
    {
        FieldInfo hookField = windowType.GetField(
            "mouseHook",
            BindingFlags.Instance | BindingFlags.NonPublic);
        FieldInfo queueField = windowType.GetField(
            "nativeInputQueue",
            BindingFlags.Instance | BindingFlags.NonPublic);
        FieldInfo pendingField = windowType.GetField(
            "pendingNativeMouseMove",
            BindingFlags.Instance | BindingFlags.NonPublic);
        FieldInfo threadField = windowType.GetField(
            "nativeInputThread",
            BindingFlags.Instance | BindingFlags.NonPublic);
        FieldInfo sessionField = windowType.GetField(
            "nativeInputSession",
            BindingFlags.Instance | BindingFlags.NonPublic);
        object queue = queueField.GetValue(window);
        int queueCount = (int)queue.GetType().GetProperty("Count").GetValue(queue, null);
        Thread worker = (Thread)threadField.GetValue(window);
        object session = sessionField.GetValue(window);
        RectangleF bounds = (RectangleF)session.GetType().GetField("RemoteBounds").GetValue(session);
        return String.Format(
            "hook={0} foreground={1} window={2} queue={3} pendingMove={4} worker={5} bounds={6}",
            hookField.GetValue(window),
            GetForegroundWindow(),
            window.Handle,
            queueCount,
            pendingField.GetValue(window) != null,
            worker == null ? "null" : worker.ThreadState.ToString(),
            bounds);
    }

    private static string ReadHeader(NetworkStream stream)
    {
        MemoryStream buffer = new MemoryStream();
        int matched = 0;
        byte[] ending = { 13, 10, 13, 10 };
        while (buffer.Length < 16384)
        {
            int value = stream.ReadByte();
            if (value < 0) throw new EndOfStreamException();
            buffer.WriteByte((byte)value);
            if (value == ending[matched])
            {
                matched += 1;
                if (matched == ending.Length)
                    return Encoding.ASCII.GetString(buffer.ToArray());
            }
            else
            {
                matched = value == ending[0] ? 1 : 0;
            }
        }
        throw new InvalidDataException("Native input header was too large.");
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
        FieldInfo shownKey = typeof(Form).GetField(
            "EVENT_SHOWN",
            BindingFlags.NonPublic | BindingFlags.Static);
        PropertyInfo eventsProperty = typeof(Component).GetProperty(
            "Events",
            BindingFlags.NonPublic | BindingFlags.Instance);
        if (shownKey == null || eventsProperty == null)
            throw new InvalidOperationException("WinForms Shown event metadata was not found.");
        EventHandlerList events = (EventHandlerList)eventsProperty.GetValue(window, null);
        object key = shownKey.GetValue(null);
        Delegate handler = events[key];
        if (handler != null)
            events.RemoveHandler(key, handler);
    }
}
