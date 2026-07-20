using System;
using System.Collections;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace WindowsLANRemoteControlHost
{
    internal static class Program
    {
        [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
        private static extern int SetCurrentProcessExplicitAppUserModelID(string appId);

        [STAThread]
        private static int Main(string[] args)
        {
            Uri url;
            if (!TryReadUrl(args, out url))
            {
                MessageBox.Show(
                    "The remote control session URL is invalid.",
                    "LAN Remote",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error);
                return 2;
            }

            SetCurrentProcessExplicitAppUserModelID("EmpK1019.WindowsLANRemote");
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new ControlWindow(url));
            return 0;
        }

        private static bool TryReadUrl(string[] args, out Uri url)
        {
            url = null;
            string value = null;
            for (int index = 0; args != null && index < args.Length - 1; index++)
            {
                if (String.Equals(args[index], "--url", StringComparison.OrdinalIgnoreCase))
                {
                    value = args[index + 1];
                    break;
                }
            }

            Uri parsed;
            if (!Uri.TryCreate(value, UriKind.Absolute, out parsed) ||
                !String.Equals(parsed.Scheme, Uri.UriSchemeHttp, StringComparison.OrdinalIgnoreCase) ||
                !String.Equals(parsed.Host, "127.0.0.1", StringComparison.Ordinal) ||
                parsed.Port < 1 || parsed.Port > 65535)
            {
                return false;
            }

            System.Collections.Specialized.NameValueCollection query = HttpUtility.ParseQueryString(parsed.Query);
            string remote = query.Get("remote");
            string handoff = query.Get("handoff");
            bool isRemoteWindow = String.Equals(remote, "1", StringComparison.Ordinal);
            if ((!String.IsNullOrEmpty(remote) && !isRemoteWindow) ||
                (isRemoteWindow && (String.IsNullOrWhiteSpace(handoff) || handoff.Length < 16 || handoff.Length > 64)) ||
                (!isRemoteWindow && !String.IsNullOrEmpty(handoff)))
            {
                return false;
            }

            url = parsed;
            return true;
        }
    }

    internal sealed class ControlWindow : Form
    {
        private const int WmNcLButtonDown = 0x00A1;
        private const int HtCaption = 0x0002;
        private const int WhKeyboardLl = 13;
        private const int WhMouseLl = 14;
        private const int WmKeyDown = 0x0100;
        private const int WmKeyUp = 0x0101;
        private const int WmSysKeyDown = 0x0104;
        private const int WmSysKeyUp = 0x0105;
        private const int WmMouseMove = 0x0200;
        private const int WmLButtonDown = 0x0201;
        private const int WmLButtonUp = 0x0202;
        private const int WmRButtonDown = 0x0204;
        private const int WmRButtonUp = 0x0205;
        private const int WmMButtonDown = 0x0207;
        private const int WmMButtonUp = 0x0208;
        private const int WmMouseWheel = 0x020A;
        private const int WmXButtonDown = 0x020B;
        private const int WmXButtonUp = 0x020C;
        private const int WmMouseHWheel = 0x020E;
        private const uint LlkhfExtended = 0x00000001;
        private const uint LlkhfInjected = 0x00000010;
        private const uint LlmhfInjected = 0x00000001;
        private const ulong RemoteInputExtraInfo = 0x4C414E52;

        private readonly Uri sessionUrl;
        private readonly WebView2 browser;
        private readonly JavaScriptSerializer serializer = new JavaScriptSerializer();
        private readonly bool remoteWindow;
        private NotifyIcon trayIcon;
        private ContextMenuStrip trayMenu;
        private bool closeToTray;
        private bool forceExit;
        private bool fullscreen;
        private bool maximized;
        private Rectangle restoredBounds;
        private bool restoredMaximized;
        private bool restoredTopMost;
        private IntPtr keyboardHook = IntPtr.Zero;
        private LowLevelKeyboardProc keyboardHookProc;
        private IntPtr mouseHook = IntPtr.Zero;
        private LowLevelMouseProc mouseHookProc;
        private volatile bool keyboardCaptureEnabled;
        private readonly object nativeInputLock = new object();
        private readonly Queue<NativeInputMessage> nativeInputQueue = new Queue<NativeInputMessage>();
        private readonly AutoResetEvent nativeInputSignal = new AutoResetEvent(false);
        private readonly Dictionary<string, NativeKeyEvent> nativePressedKeys =
            new Dictionary<string, NativeKeyEvent>(StringComparer.Ordinal);
        private readonly HashSet<int> nativePressedButtons = new HashSet<int>();
        private NativeInputMessage pendingNativeMouseMove;
        private NativeInputSession nativeInputSession;
        private Thread nativeInputThread;
        private volatile bool nativeInputStopping;
        private int lastRemoteX;
        private int lastRemoteY;
        private bool nativeTransportFailed;

        [DllImport("user32.dll")]
        private static extern bool ReleaseCapture();

        [DllImport("user32.dll")]
        private static extern IntPtr SendMessage(IntPtr window, int message, IntPtr wParam, IntPtr lParam);

        [DllImport("user32.dll", SetLastError = true)]
        private static extern IntPtr SetWindowsHookEx(int hookId, LowLevelKeyboardProc callback, IntPtr module, uint threadId);

        [DllImport("user32.dll", EntryPoint = "SetWindowsHookEx", SetLastError = true)]
        private static extern IntPtr SetWindowsMouseHookEx(int hookId, LowLevelMouseProc callback, IntPtr module, uint threadId);

        [DllImport("user32.dll", SetLastError = true)]
        private static extern bool UnhookWindowsHookEx(IntPtr hook);

        [DllImport("user32.dll")]
        private static extern IntPtr CallNextHookEx(IntPtr hook, int code, IntPtr wParam, IntPtr lParam);

        [DllImport("user32.dll")]
        private static extern IntPtr GetForegroundWindow();

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        private static extern IntPtr GetModuleHandle(string moduleName);

        private delegate IntPtr LowLevelKeyboardProc(int code, IntPtr wParam, IntPtr lParam);
        private delegate IntPtr LowLevelMouseProc(int code, IntPtr wParam, IntPtr lParam);

        [StructLayout(LayoutKind.Sequential)]
        private struct LowLevelKeyboardInput
        {
            public uint VirtualKey;
            public uint ScanCode;
            public uint Flags;
            public uint Time;
            public UIntPtr ExtraInfo;
        }

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

        private sealed class NativeKeyEvent
        {
            public uint ScanCode;
            public bool Extended;
            public bool KeyDown;
        }

        private sealed class NativeInputSession
        {
            public Uri Endpoint;
            public string Token;
            public string Monitor;
            public RectangleF RemoteBounds;
            public RectangleF[] Exclusions;
            public int RemoteWidth;
            public int RemoteHeight;
            public bool KeyboardEnabled;
            public bool Suspended;
        }

        private sealed class NativeInputMessage
        {
            public NativeInputSession Session;
            public Dictionary<string, object> Payload;
            public bool MouseMove;
        }

        private sealed class NativeInputTransport : IDisposable
        {
            private TcpClient client;
            private NetworkStream stream;
            private string connectedKey = String.Empty;
            private string unsupportedKey = String.Empty;
            private DateTime unsupportedUntil = DateTime.MinValue;

            public bool Send(NativeInputSession session, string json)
            {
                byte[] payload = Encoding.UTF8.GetBytes(json);
                string key = session.Endpoint.GetLeftPart(UriPartial.Authority) + "|" + session.Token;
                if (payload.Length > 4096)
                {
                    return false;
                }

                if (!String.Equals(key, unsupportedKey, StringComparison.Ordinal) ||
                    DateTime.UtcNow >= unsupportedUntil)
                {
                    for (int attempt = 0; attempt < 2; attempt++)
                    {
                        try
                        {
                            if (!String.Equals(connectedKey, key, StringComparison.Ordinal))
                            {
                                CloseStream();
                            }
                            if (stream == null && !OpenStream(session, key))
                            {
                                break;
                            }
                            WriteFrame(payload);
                            int acknowledgement = stream.ReadByte();
                            if (acknowledgement == 0)
                            {
                                return true;
                            }
                            CloseStream();
                            if (acknowledgement >= 0)
                            {
                                return false;
                            }
                        }
                        catch (IOException)
                        {
                            CloseStream();
                        }
                        catch (SocketException)
                        {
                            CloseStream();
                        }
                    }
                }
                return SendHttp(session, payload);
            }

            private bool OpenStream(NativeInputSession session, string key)
            {
                TcpClient candidate = new TcpClient();
                candidate.NoDelay = true;
                try
                {
                    IAsyncResult connect = candidate.BeginConnect(session.Endpoint.Host, session.Endpoint.Port, null, null);
                    try
                    {
                        if (!connect.AsyncWaitHandle.WaitOne(900))
                        {
                            candidate.Close();
                            return false;
                        }
                        candidate.EndConnect(connect);
                    }
                    finally
                    {
                        connect.AsyncWaitHandle.Close();
                    }
                }
                catch
                {
                    candidate.Close();
                    throw;
                }

                NetworkStream candidateStream = candidate.GetStream();
                candidateStream.ReadTimeout = 1800;
                candidateStream.WriteTimeout = 1800;
                string host = session.Endpoint.HostNameType == UriHostNameType.IPv6
                    ? "[" + session.Endpoint.Host + "]"
                    : session.Endpoint.Host;
                string request =
                    "CONNECT /input-stream HTTP/1.1\r\n" +
                    "Host: " + host + ":" + session.Endpoint.Port.ToString() + "\r\n" +
                    "X-Remote-Token: " + session.Token + "\r\n" +
                    "Connection: keep-alive\r\n" +
                    "User-Agent: Windows-LAN-Remote-Native/1\r\n\r\n";
                byte[] requestBytes = Encoding.ASCII.GetBytes(request);
                candidateStream.Write(requestBytes, 0, requestBytes.Length);
                candidateStream.Flush();

                string response = ReadHeader(candidateStream);
                string firstLine = response.Split(new[] { "\r\n" }, StringSplitOptions.None)[0];
                if (firstLine.IndexOf(" 200 ", StringComparison.Ordinal) < 0)
                {
                    candidateStream.Dispose();
                    candidate.Close();
                    unsupportedKey = key;
                    unsupportedUntil = DateTime.UtcNow.AddSeconds(45);
                    return false;
                }

                client = candidate;
                stream = candidateStream;
                connectedKey = key;
                unsupportedKey = String.Empty;
                unsupportedUntil = DateTime.MinValue;
                return true;
            }

            private static string ReadHeader(NetworkStream input)
            {
                MemoryStream buffer = new MemoryStream();
                int matched = 0;
                while (buffer.Length < 8192)
                {
                    int value = input.ReadByte();
                    if (value < 0)
                    {
                        throw new IOException("The native input handshake ended early.");
                    }
                    buffer.WriteByte((byte)value);
                    if ((matched == 0 || matched == 2) && value == '\r')
                    {
                        matched++;
                    }
                    else if ((matched == 1 || matched == 3) && value == '\n')
                    {
                        matched++;
                        if (matched == 4)
                        {
                            return Encoding.ASCII.GetString(buffer.ToArray());
                        }
                    }
                    else
                    {
                        matched = value == '\r' ? 1 : 0;
                    }
                }
                throw new IOException("The native input handshake was too large.");
            }

            private void WriteFrame(byte[] payload)
            {
                byte[] header = new byte[4];
                int length = payload.Length;
                header[0] = (byte)(length >> 24);
                header[1] = (byte)(length >> 16);
                header[2] = (byte)(length >> 8);
                header[3] = (byte)length;
                stream.Write(header, 0, header.Length);
                stream.Write(payload, 0, payload.Length);
                stream.Flush();
            }

            private static bool SendHttp(NativeInputSession session, byte[] payload)
            {
                try
                {
                    HttpWebRequest request = (HttpWebRequest)WebRequest.Create(session.Endpoint);
                    request.Method = "POST";
                    request.ContentType = "application/json";
                    request.ContentLength = payload.Length;
                    request.Headers["X-Remote-Token"] = session.Token;
                    request.UserAgent = "Windows-LAN-Remote-Native/1";
                    request.Proxy = null;
                    request.KeepAlive = false;
                    request.Timeout = 1800;
                    request.ReadWriteTimeout = 1800;
                    using (Stream output = request.GetRequestStream())
                    {
                        output.Write(payload, 0, payload.Length);
                    }
                    using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
                    {
                        return (int)response.StatusCode >= 200 && (int)response.StatusCode < 300;
                    }
                }
                catch (WebException)
                {
                    return false;
                }
                catch (IOException)
                {
                    return false;
                }
            }

            private void CloseStream()
            {
                connectedKey = String.Empty;
                if (stream != null)
                {
                    stream.Dispose();
                    stream = null;
                }
                if (client != null)
                {
                    client.Close();
                    client = null;
                }
            }

            public void Dispose()
            {
                CloseStream();
            }
        }

        public ControlWindow(Uri url)
        {
            sessionUrl = url;
            System.Collections.Specialized.NameValueCollection query = HttpUtility.ParseQueryString(url.Query);
            remoteWindow = String.Equals(query.Get("remote"), "1", StringComparison.Ordinal);
            closeToTray = !remoteWindow;
            bool startMaximized = String.Equals(query.Get("maximized"), "1", StringComparison.Ordinal);
            Text = remoteWindow ? "LAN Remote · 远程控制" : "LAN Remote";
            try
            {
                Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
            }
            catch
            {
                // The embedded executable icon remains the Windows fallback.
            }
            StartPosition = FormStartPosition.CenterScreen;
            FormBorderStyle = FormBorderStyle.None;
            BackColor = Color.FromArgb(15, 16, 20);
            ClientSize = remoteWindow ? new Size(1280, 800) : new Size(1200, 760);
            MinimumSize = remoteWindow ? new Size(720, 480) : new Size(920, 600);
            KeyPreview = true;
            if (startMaximized)
            {
                WindowState = FormWindowState.Maximized;
                maximized = true;
            }

            browser = new WebView2();
            browser.Dock = DockStyle.Fill;
            browser.DefaultBackgroundColor = BackColor;
            Controls.Add(browser);
            if (!remoteWindow)
            {
                InitializeTray();
            }
            else
            {
                Deactivate += delegate { ReleaseNativePressedInputs(); };
            }

            Shown += async delegate
            {
                try
                {
                    if (remoteWindow)
                    {
                        InitializeKeyboardHook();
                        InitializeMouseHook();
                        StartNativeInputWorker();
                    }
                    await InitializeBrowser();
                }
                catch (Exception ex)
                {
                    MessageBox.Show(
                        "The remote control window could not start.\n\n" + ex.Message,
                        "LAN Remote",
                        MessageBoxButtons.OK,
                        MessageBoxIcon.Error);
                    forceExit = true;
                    Close();
                }
            };
        }

        private void InitializeKeyboardHook()
        {
            if (!remoteWindow || keyboardHook != IntPtr.Zero)
            {
                return;
            }
            keyboardHookProc = KeyboardHookCallback;
            keyboardHook = SetWindowsHookEx(WhKeyboardLl, keyboardHookProc, GetModuleHandle(null), 0);
        }

        private bool SetKeyboardCapture(bool enabled)
        {
            lock (nativeInputLock)
            {
                keyboardCaptureEnabled =
                    remoteWindow &&
                    enabled &&
                    keyboardHook != IntPtr.Zero &&
                    nativeInputSession != null &&
                    nativeInputSession.KeyboardEnabled &&
                    !nativeInputSession.Suspended;
            }
            if (!keyboardCaptureEnabled)
            {
                ReleaseNativePressedKeys();
            }
            return keyboardCaptureEnabled;
        }

        private IntPtr KeyboardHookCallback(int code, IntPtr wParam, IntPtr lParam)
        {
            int message = wParam.ToInt32();
            bool keyDown = message == WmKeyDown || message == WmSysKeyDown;
            bool keyUp = message == WmKeyUp || message == WmSysKeyUp;
            if (
                code >= 0 &&
                (keyDown || keyUp) &&
                keyboardCaptureEnabled &&
                IsHandleCreated &&
                !IsDisposed &&
                GetForegroundWindow() == Handle)
            {
                LowLevelKeyboardInput input = (LowLevelKeyboardInput)Marshal.PtrToStructure(
                    lParam,
                    typeof(LowLevelKeyboardInput));
                if (
                    (input.Flags & LlkhfInjected) != 0 &&
                    input.ExtraInfo.ToUInt64() == RemoteInputExtraInfo)
                {
                    return CallNextHookEx(keyboardHook, code, wParam, lParam);
                }
                ForwardNativeKey(input.ScanCode, (input.Flags & LlkhfExtended) != 0, keyDown);
                return new IntPtr(1);
            }
            return CallNextHookEx(keyboardHook, code, wParam, lParam);
        }

        private void ForwardNativeKey(uint scanCode, bool extended, bool keyDown)
        {
            if (scanCode < 1 || scanCode > 255)
            {
                return;
            }
            lock (nativeInputLock)
            {
                NativeInputSession session = nativeInputSession;
                if (session == null || !session.KeyboardEnabled || !keyboardCaptureEnabled)
                {
                    return;
                }
                string keyId = scanCode.ToString() + ":" + (extended ? "1" : "0");
                if (keyDown)
                {
                    nativePressedKeys[keyId] = new NativeKeyEvent
                    {
                        ScanCode = scanCode,
                        Extended = extended,
                        KeyDown = true
                    };
                }
                else
                {
                    nativePressedKeys.Remove(keyId);
                }
                EnqueueNativeInputUnsafe(
                    session,
                    new Dictionary<string, object>
                    {
                        { "type", keyDown ? "native_key_down" : "native_key_up" },
                        { "scan_code", (int)scanCode },
                        { "extended", extended }
                    },
                    false);
            }
        }

        private void InitializeMouseHook()
        {
            if (!remoteWindow || mouseHook != IntPtr.Zero)
            {
                return;
            }
            mouseHookProc = MouseHookCallback;
            mouseHook = SetWindowsMouseHookEx(WhMouseLl, mouseHookProc, GetModuleHandle(null), 0);
        }

        private IntPtr MouseHookCallback(int code, IntPtr wParam, IntPtr lParam)
        {
            if (
                code < 0 ||
                !IsHandleCreated ||
                IsDisposed ||
                GetForegroundWindow() != Handle)
            {
                return CallNextHookEx(mouseHook, code, wParam, lParam);
            }
            LowLevelMouseInput input = (LowLevelMouseInput)Marshal.PtrToStructure(
                lParam,
                typeof(LowLevelMouseInput));
            if (ShouldPassInjectedMouseInput(input.Flags))
            {
                return CallNextHookEx(mouseHook, code, wParam, lParam);
            }

            int message = wParam.ToInt32();
            bool swallow = false;
            lock (nativeInputLock)
            {
                NativeInputSession session = nativeInputSession;
                if (
                    session == null ||
                    session.Suspended ||
                    session.RemoteBounds.Width < 1 ||
                    session.RemoteBounds.Height < 1)
                {
                    return CallNextHookEx(mouseHook, code, wParam, lParam);
                }
                bool inside = session.RemoteBounds.Contains(input.Point.X, input.Point.Y) &&
                    !PointInExclusion(session, input.Point.X, input.Point.Y);
                bool dragging = nativePressedButtons.Count > 0;
                if (!inside && !dragging)
                {
                    return CallNextHookEx(mouseHook, code, wParam, lParam);
                }

                int remoteX;
                int remoteY;
                MapRemotePoint(session, input.Point.X, input.Point.Y, out remoteX, out remoteY);
                lastRemoteX = remoteX;
                lastRemoteY = remoteY;

                if (message == WmMouseMove)
                {
                    EnqueueNativeInputUnsafe(
                        session,
                        MousePayload("mouse_move", remoteX, remoteY, 0, 0, session.Monitor),
                        true);
                }
                else if (message == WmMouseWheel && inside)
                {
                    int wheel = unchecked((short)((input.MouseData >> 16) & 0xFFFF));
                    EnqueueNativeInputUnsafe(
                        session,
                        MousePayload("mouse_wheel", remoteX, remoteY, 0, -wheel, session.Monitor),
                        false);
                    swallow = true;
                }
                else if (message == WmMouseHWheel && inside)
                {
                    int wheel = unchecked((short)((input.MouseData >> 16) & 0xFFFF));
                    EnqueueNativeInputUnsafe(
                        session,
                        MousePayload("mouse_hwheel", remoteX, remoteY, 0, wheel, session.Monitor),
                        false);
                    swallow = true;
                }
                else
                {
                    int button;
                    bool down;
                    if (TryMouseButton(message, input.MouseData, out button, out down))
                    {
                        bool wasPressed = nativePressedButtons.Contains(button);
                        if (down && inside)
                        {
                            nativePressedButtons.Add(button);
                            EnqueueNativeInputUnsafe(
                                session,
                                MousePayload("mouse_down", remoteX, remoteY, button, 0, session.Monitor),
                                false);
                            swallow = true;
                        }
                        else if (!down && (inside || wasPressed))
                        {
                            nativePressedButtons.Remove(button);
                            EnqueueNativeInputUnsafe(
                                session,
                                MousePayload("mouse_up", remoteX, remoteY, button, 0, session.Monitor),
                                false);
                            swallow = true;
                        }
                    }
                }
            }
            return swallow
                ? new IntPtr(1)
                : CallNextHookEx(mouseHook, code, wParam, lParam);
        }

        private static bool PointInExclusion(NativeInputSession session, int x, int y)
        {
            RectangleF[] exclusions = session.Exclusions ?? new RectangleF[0];
            for (int index = 0; index < exclusions.Length; index++)
            {
                if (exclusions[index].Contains(x, y))
                {
                    return true;
                }
            }
            return false;
        }

        private static void MapRemotePoint(
            NativeInputSession session,
            int screenX,
            int screenY,
            out int remoteX,
            out int remoteY)
        {
            double normalizedX = (screenX - session.RemoteBounds.Left) / session.RemoteBounds.Width;
            double normalizedY = (screenY - session.RemoteBounds.Top) / session.RemoteBounds.Height;
            normalizedX = Math.Max(0.0, Math.Min(1.0, normalizedX));
            normalizedY = Math.Max(0.0, Math.Min(1.0, normalizedY));
            remoteX = (int)Math.Round(normalizedX * Math.Max(0, session.RemoteWidth - 1));
            remoteY = (int)Math.Round(normalizedY * Math.Max(0, session.RemoteHeight - 1));
        }

        private static Dictionary<string, object> MousePayload(
            string type,
            int x,
            int y,
            int button,
            int delta,
            string monitor)
        {
            return new Dictionary<string, object>
            {
                { "type", type },
                { "x", x },
                { "y", y },
                { "button", button },
                { "delta", delta },
                { "monitor", monitor ?? "all" }
            };
        }

        private static bool TryMouseButton(int message, uint mouseData, out int button, out bool down)
        {
            button = 0;
            down = false;
            switch (message)
            {
                case WmLButtonDown:
                    down = true;
                    return true;
                case WmLButtonUp:
                    return true;
                case WmMButtonDown:
                    button = 1;
                    down = true;
                    return true;
                case WmMButtonUp:
                    button = 1;
                    return true;
                case WmRButtonDown:
                    button = 2;
                    down = true;
                    return true;
                case WmRButtonUp:
                    button = 2;
                    return true;
                case WmXButtonDown:
                case WmXButtonUp:
                    button = ((mouseData >> 16) & 0xFFFF) == 1 ? 3 : 4;
                    down = message == WmXButtonDown;
                    return true;
                default:
                    return false;
            }
        }

        private static bool ShouldPassInjectedMouseInput(uint flags)
        {
            // Precision touchpads and vendor mouse utilities can surface clicks
            // and wheel gestures as injected input. Let WebView receive those
            // events instead of swallowing them in the low-level hook.
            return (flags & LlmhfInjected) != 0;
        }

        private void StartNativeInputWorker()
        {
            if (!remoteWindow || nativeInputThread != null)
            {
                return;
            }
            nativeInputStopping = false;
            nativeInputThread = new Thread(NativeInputLoop);
            nativeInputThread.IsBackground = true;
            nativeInputThread.Name = "LAN Remote native input";
            nativeInputThread.Start();
        }

        private void NativeInputLoop()
        {
            JavaScriptSerializer inputSerializer = new JavaScriptSerializer();
            using (NativeInputTransport transport = new NativeInputTransport())
            {
                while (true)
                {
                    NativeInputMessage message = null;
                    lock (nativeInputLock)
                    {
                        if (nativeInputQueue.Count > 0)
                        {
                            message = nativeInputQueue.Dequeue();
                        }
                        else if (pendingNativeMouseMove != null)
                        {
                            message = pendingNativeMouseMove;
                            pendingNativeMouseMove = null;
                        }
                    }
                    if (message == null)
                    {
                        if (nativeInputStopping)
                        {
                            break;
                        }
                        nativeInputSignal.WaitOne(250);
                        continue;
                    }
                    bool sent = false;
                    try
                    {
                        sent = transport.Send(message.Session, inputSerializer.Serialize(message.Payload));
                    }
                    catch (Exception)
                    {
                        sent = false;
                    }
                    NotifyNativeInputTransport(sent);
                }
            }
        }

        private void EnqueueNativeInputUnsafe(
            NativeInputSession session,
            Dictionary<string, object> payload,
            bool mouseMove)
        {
            NativeInputMessage message = new NativeInputMessage
            {
                Session = session,
                Payload = payload,
                MouseMove = mouseMove
            };
            if (mouseMove)
            {
                pendingNativeMouseMove = message;
            }
            else if (nativeInputQueue.Count < 512)
            {
                nativeInputQueue.Enqueue(message);
            }
            nativeInputSignal.Set();
        }

        private void NotifyNativeInputTransport(bool sent)
        {
            if (sent && !nativeTransportFailed)
            {
                return;
            }
            if (!sent && nativeTransportFailed)
            {
                return;
            }
            nativeTransportFailed = !sent;
            if (!IsHandleCreated || IsDisposed)
            {
                return;
            }
            try
            {
                BeginInvoke(new Action(delegate
                {
                    if (browser.CoreWebView2 == null)
                    {
                        return;
                    }
                    string script = "window.__lanNativeInputStatus&&window.__lanNativeInputStatus(" +
                        (sent ? "true" : "false") + ");";
                    browser.CoreWebView2.ExecuteScriptAsync(script);
                }));
            }
            catch (InvalidOperationException)
            {
            }
        }

        private bool ConfigureNativeInput(object payload)
        {
            Dictionary<string, object> values = payload as Dictionary<string, object>;
            if (values == null || !ReadBoolean(values, "enabled", false))
            {
                ReleaseNativePressedInputs();
                lock (nativeInputLock)
                {
                    nativeInputSession = null;
                    pendingNativeMouseMove = null;
                }
                keyboardCaptureEnabled = false;
                return true;
            }

            string endpointValue = ReadString(values, "endpoint");
            string token = ReadString(values, "token");
            string monitor = ReadString(values, "monitor");
            Uri endpoint;
            if (
                !remoteWindow ||
                !Uri.TryCreate(endpointValue, UriKind.Absolute, out endpoint) ||
                !String.Equals(endpoint.Scheme, Uri.UriSchemeHttp, StringComparison.OrdinalIgnoreCase) ||
                !String.Equals(endpoint.AbsolutePath, "/input", StringComparison.Ordinal) ||
                !String.IsNullOrEmpty(endpoint.UserInfo) ||
                endpoint.Port < 1 ||
                endpoint.Port > 65535 ||
                String.IsNullOrWhiteSpace(token) ||
                token.Length < 16 ||
                token.Length > 256 ||
                token.IndexOfAny(new[] { '\r', '\n' }) >= 0)
            {
                return false;
            }

            int remoteWidth = ReadInteger(values, "remote_width", 0);
            int remoteHeight = ReadInteger(values, "remote_height", 0);
            RectangleF remoteBounds = RectangleF.Empty;
            if (remoteWidth > 0 && remoteHeight > 0)
            {
                remoteBounds = BrowserRectangleToScreen(
                    ReadDouble(values, "content_left", 0),
                    ReadDouble(values, "content_top", 0),
                    ReadDouble(values, "content_width", 0),
                    ReadDouble(values, "content_height", 0));
            }
            RectangleF[] exclusions = ReadExclusions(values);
            NativeInputSession session = new NativeInputSession
            {
                Endpoint = endpoint,
                Token = token,
                Monitor = String.IsNullOrWhiteSpace(monitor) ? "all" : monitor,
                RemoteBounds = remoteBounds,
                Exclusions = exclusions,
                RemoteWidth = remoteWidth,
                RemoteHeight = remoteHeight,
                KeyboardEnabled = ReadBoolean(values, "keyboard_enabled", false),
                Suspended = ReadBoolean(values, "suspended", false)
            };
            lock (nativeInputLock)
            {
                nativeInputSession = session;
                pendingNativeMouseMove = null;
            }
            keyboardCaptureEnabled =
                session.KeyboardEnabled &&
                keyboardHook != IntPtr.Zero &&
                !session.Suspended;
            return mouseHook != IntPtr.Zero && (!session.KeyboardEnabled || keyboardHook != IntPtr.Zero);
        }

        private RectangleF BrowserRectangleToScreen(double left, double top, double width, double height)
        {
            if (
                Double.IsNaN(left) ||
                Double.IsNaN(top) ||
                Double.IsNaN(width) ||
                Double.IsNaN(height) ||
                width <= 0 ||
                height <= 0 ||
                width > 65536 ||
                height > 65536)
            {
                return RectangleF.Empty;
            }
            Point topLeft = browser.PointToScreen(new Point(
                (int)Math.Round(left),
                (int)Math.Round(top)));
            Point bottomRight = browser.PointToScreen(new Point(
                (int)Math.Round(left + width),
                (int)Math.Round(top + height)));
            return RectangleF.FromLTRB(topLeft.X, topLeft.Y, bottomRight.X, bottomRight.Y);
        }

        private RectangleF[] ReadExclusions(Dictionary<string, object> values)
        {
            object raw;
            if (!values.TryGetValue("exclusions", out raw) || raw == null || raw is string)
            {
                return new RectangleF[0];
            }
            IEnumerable items = raw as IEnumerable;
            if (items == null)
            {
                return new RectangleF[0];
            }
            List<RectangleF> result = new List<RectangleF>();
            foreach (object item in items)
            {
                Dictionary<string, object> rectangle = item as Dictionary<string, object>;
                if (rectangle == null)
                {
                    continue;
                }
                RectangleF screenRectangle = BrowserRectangleToScreen(
                    ReadDouble(rectangle, "left", 0),
                    ReadDouble(rectangle, "top", 0),
                    ReadDouble(rectangle, "width", 0),
                    ReadDouble(rectangle, "height", 0));
                if (!screenRectangle.IsEmpty)
                {
                    result.Add(screenRectangle);
                }
                if (result.Count >= 16)
                {
                    break;
                }
            }
            return result.ToArray();
        }

        private static string ReadString(Dictionary<string, object> values, string key)
        {
            object value;
            return values.TryGetValue(key, out value) ? Convert.ToString(value) ?? String.Empty : String.Empty;
        }

        private static bool ReadBoolean(Dictionary<string, object> values, string key, bool fallback)
        {
            object value;
            if (!values.TryGetValue(key, out value) || value == null)
            {
                return fallback;
            }
            if (value is bool)
            {
                return (bool)value;
            }
            bool parsed;
            return Boolean.TryParse(Convert.ToString(value), out parsed) ? parsed : fallback;
        }

        private static double ReadDouble(Dictionary<string, object> values, string key, double fallback)
        {
            object value;
            if (!values.TryGetValue(key, out value) || value == null)
            {
                return fallback;
            }
            try
            {
                return Convert.ToDouble(value, System.Globalization.CultureInfo.InvariantCulture);
            }
            catch (FormatException)
            {
                return fallback;
            }
            catch (InvalidCastException)
            {
                return fallback;
            }
            catch (OverflowException)
            {
                return fallback;
            }
        }

        private static int ReadInteger(Dictionary<string, object> values, string key, int fallback)
        {
            double value = ReadDouble(values, key, fallback);
            if (Double.IsNaN(value) || value < 0 || value > 65536)
            {
                return fallback;
            }
            return (int)Math.Round(value);
        }

        private void ReleaseNativePressedKeys()
        {
            lock (nativeInputLock)
            {
                NativeInputSession session = nativeInputSession;
                if (session == null)
                {
                    nativePressedKeys.Clear();
                    return;
                }
                foreach (NativeKeyEvent key in nativePressedKeys.Values)
                {
                    EnqueueNativeInputUnsafe(
                        session,
                        new Dictionary<string, object>
                        {
                            { "type", "native_key_up" },
                            { "scan_code", (int)key.ScanCode },
                            { "extended", key.Extended }
                        },
                        false);
                }
                nativePressedKeys.Clear();
            }
        }

        private void ReleaseNativePressedInputs()
        {
            lock (nativeInputLock)
            {
                NativeInputSession session = nativeInputSession;
                pendingNativeMouseMove = null;
                if (session == null)
                {
                    nativePressedKeys.Clear();
                    nativePressedButtons.Clear();
                    return;
                }
                foreach (NativeKeyEvent key in nativePressedKeys.Values)
                {
                    EnqueueNativeInputUnsafe(
                        session,
                        new Dictionary<string, object>
                        {
                            { "type", "native_key_up" },
                            { "scan_code", (int)key.ScanCode },
                            { "extended", key.Extended }
                        },
                        false);
                }
                foreach (int button in nativePressedButtons)
                {
                    EnqueueNativeInputUnsafe(
                        session,
                        MousePayload(
                            "mouse_up",
                            lastRemoteX,
                            lastRemoteY,
                            button,
                            0,
                            session.Monitor),
                        false);
                }
                nativePressedKeys.Clear();
                nativePressedButtons.Clear();
            }
        }

        private void InitializeTray()
        {
            trayMenu = new ContextMenuStrip();
            ToolStripMenuItem openItem = new ToolStripMenuItem("打开 LAN Remote");
            openItem.Click += delegate { RestoreFromTray(); };
            ToolStripMenuItem exitItem = new ToolStripMenuItem("退出 LAN Remote");
            exitItem.Click += delegate { ExitFromTray(); };
            trayMenu.Items.Add(openItem);
            trayMenu.Items.Add(new ToolStripSeparator());
            trayMenu.Items.Add(exitItem);

            trayIcon = new NotifyIcon();
            trayIcon.Text = "LAN Remote";
            trayIcon.Icon = Icon ?? SystemIcons.Application;
            trayIcon.ContextMenuStrip = trayMenu;
            trayIcon.Visible = false;
            trayIcon.DoubleClick += delegate { RestoreFromTray(); };
        }

        private void SetCloseToTray(bool enabled)
        {
            closeToTray = !remoteWindow && enabled;
        }

        private void HideToTray()
        {
            if (remoteWindow || trayIcon == null)
            {
                return;
            }
            if (fullscreen)
            {
                ToggleFullscreen();
            }
            trayIcon.Visible = true;
            ShowInTaskbar = false;
            Hide();
        }

        private void RestoreFromTray()
        {
            if (remoteWindow || trayIcon == null)
            {
                return;
            }
            trayIcon.Visible = false;
            ShowInTaskbar = true;
            if (WindowState == FormWindowState.Minimized)
            {
                WindowState = FormWindowState.Normal;
            }
            Show();
            Activate();
            BringToFront();
        }

        private void ExitFromTray()
        {
            forceExit = true;
            if (trayIcon != null)
            {
                trayIcon.Visible = false;
            }
            Close();
        }

        private async Task InitializeBrowser()
        {
            string dataFolder = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "LAN Remote",
                "ControlHostWebView2");
            Directory.CreateDirectory(dataFolder);

            CoreWebView2Environment environment = await CoreWebView2Environment.CreateAsync(null, dataFolder);
            await browser.EnsureCoreWebView2Async(environment);
            browser.CoreWebView2.Settings.AreDefaultContextMenusEnabled = false;
            browser.CoreWebView2.Settings.AreDevToolsEnabled = false;
            browser.CoreWebView2.Settings.IsStatusBarEnabled = false;
            if (remoteWindow)
            {
                browser.CoreWebView2.Settings.AreBrowserAcceleratorKeysEnabled = false;
            }
            browser.CoreWebView2.WebMessageReceived += OnWebMessageReceived;
            await browser.CoreWebView2.AddScriptToExecuteOnDocumentCreatedAsync(BridgeScript);
            browser.Source = sessionUrl;
        }

        private async void OnWebMessageReceived(object sender, CoreWebView2WebMessageReceivedEventArgs args)
        {
            Dictionary<string, object> message;
            try
            {
                message = serializer.Deserialize<Dictionary<string, object>>(args.WebMessageAsJson);
            }
            catch
            {
                return;
            }

            object idValue;
            object actionValue;
            if (!message.TryGetValue("id", out idValue) || !message.TryGetValue("action", out actionValue))
            {
                return;
            }

            string id = Convert.ToString(idValue);
            string action = Convert.ToString(actionValue);
            object payload = null;
            message.TryGetValue("payload", out payload);
            object result = true;

            switch (action)
            {
                case "set_title":
                    Text = (Convert.ToString(payload) ?? "LAN Remote").Trim();
                    if (Text.Length > 160) Text = Text.Substring(0, 160);
                    break;
                case "minimize":
                    WindowState = FormWindowState.Minimized;
                    break;
                case "toggle_maximize":
                    if (fullscreen)
                    {
                        ToggleFullscreen();
                    }
                    else
                    {
                        maximized = WindowState != FormWindowState.Maximized;
                        WindowState = maximized ? FormWindowState.Maximized : FormWindowState.Normal;
                    }
                    result = maximized;
                    break;
                case "toggle_fullscreen":
                    ToggleFullscreen();
                    result = fullscreen;
                    break;
                case "window_state":
                    result = new Dictionary<string, object>
                    {
                        { "maximized", maximized },
                        { "fullscreen", fullscreen },
                        { "remote_window", remoteWindow },
                        { "close_to_tray", closeToTray }
                    };
                    break;
                case "set_close_to_tray":
                    SetCloseToTray(payload is bool && (bool)payload);
                    result = closeToTray;
                    break;
                case "set_keyboard_capture":
                    result = SetKeyboardCapture(payload is bool && (bool)payload);
                    break;
                case "configure_native_input":
                    result = ConfigureNativeInput(payload);
                    break;
                case "release_native_input":
                    ReleaseNativePressedInputs();
                    result = true;
                    break;
                case "drag":
                    ReleaseCapture();
                    SendMessage(Handle, WmNcLButtonDown, new IntPtr(HtCaption), IntPtr.Zero);
                    break;
                case "close":
                    Close();
                    return;
                default:
                    result = false;
                    break;
            }

            if (browser.CoreWebView2 != null)
            {
                string script = "window.__lanNativeResolve(" + serializer.Serialize(id) + "," +
                    serializer.Serialize(result) + ");";
                await browser.CoreWebView2.ExecuteScriptAsync(script);
            }
        }

        private void ToggleFullscreen()
        {
            if (!fullscreen)
            {
                restoredMaximized = WindowState == FormWindowState.Maximized || maximized;
                restoredBounds = restoredMaximized ? RestoreBounds : Bounds;
                restoredTopMost = TopMost;
                WindowState = FormWindowState.Normal;
                Bounds = Screen.FromControl(this).Bounds;
                TopMost = true;
                fullscreen = true;
            }
            else
            {
                TopMost = restoredTopMost;
                WindowState = FormWindowState.Normal;
                Bounds = restoredBounds;
                fullscreen = false;
                if (restoredMaximized)
                {
                    WindowState = FormWindowState.Maximized;
                }
                maximized = restoredMaximized;
            }
        }

        protected override void OnSizeChanged(EventArgs e)
        {
            base.OnSizeChanged(e);
            if (!fullscreen && WindowState != FormWindowState.Minimized)
            {
                maximized = WindowState == FormWindowState.Maximized;
            }
        }

        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            if (!remoteWindow && closeToTray && !forceExit && e.CloseReason == CloseReason.UserClosing)
            {
                e.Cancel = true;
                HideToTray();
                return;
            }
            if (trayIcon != null)
            {
                trayIcon.Visible = false;
            }
            base.OnFormClosing(e);
        }

        protected override void OnFormClosed(FormClosedEventArgs e)
        {
            keyboardCaptureEnabled = false;
            ReleaseNativePressedInputs();
            nativeInputStopping = true;
            nativeInputSignal.Set();
            bool nativeWorkerStopped = true;
            if (nativeInputThread != null && nativeInputThread.IsAlive)
            {
                nativeWorkerStopped = nativeInputThread.Join(2500);
            }
            nativeInputThread = null;
            if (keyboardHook != IntPtr.Zero)
            {
                UnhookWindowsHookEx(keyboardHook);
                keyboardHook = IntPtr.Zero;
            }
            keyboardHookProc = null;
            if (mouseHook != IntPtr.Zero)
            {
                UnhookWindowsHookEx(mouseHook);
                mouseHook = IntPtr.Zero;
            }
            mouseHookProc = null;
            if (nativeWorkerStopped)
            {
                nativeInputSignal.Dispose();
            }
            if (trayIcon != null)
            {
                trayIcon.Dispose();
                trayIcon = null;
            }
            if (trayMenu != null)
            {
                trayMenu.Dispose();
                trayMenu = null;
            }
            base.OnFormClosed(e);
        }

        private const string BridgeScript = @"
(() => {
  const pending = new Map();
  let nextId = 1;
  const call = (action, payload = null) => new Promise((resolve) => {
    const id = String(nextId++);
    pending.set(id, resolve);
    window.chrome.webview.postMessage({id, action, payload});
  });
  window.__lanNativeResolve = (id, result) => {
    const resolve = pending.get(String(id));
    if (!resolve) return;
    pending.delete(String(id));
    resolve(result);
  };
  const credentialCall = (action, payload = {}) => fetch('/api/native/credentials', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action, ...payload}),
    cache: 'no-store'
  }).then(async (response) => {
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) throw new Error(data.error || 'Credential operation failed');
    return data.result;
  });
  window.pywebview = {api: {
    set_window_title: (title) => call('set_title', String(title || 'LAN Remote')),
    minimize_window: () => call('minimize'),
    toggle_maximize_window: () => call('toggle_maximize'),
    toggle_fullscreen: () => call('toggle_fullscreen'),
    set_close_to_tray: (enabled) => call('set_close_to_tray', Boolean(enabled)),
    set_keyboard_capture: (enabled) => call('set_keyboard_capture', Boolean(enabled)),
    configure_native_input: (config) => call('configure_native_input', config || {enabled: false}),
    release_native_input: () => call('release_native_input'),
    close_window: () => call('close'),
    window_state: () => call('window_state'),
    credential_status: (deviceId) => credentialCall('status', {device_id: String(deviceId || '')}),
    load_access_password: (deviceId) => credentialCall('load_access', {device_id: String(deviceId || '')}),
    save_access_password: (deviceId, password, deviceName) => credentialCall('save_access', {
      device_id: String(deviceId || ''), password: String(password || ''), device_name: String(deviceName || '')
    }),
    clear_access_password: (deviceId) => credentialCall('clear_access', {device_id: String(deviceId || '')}),
    save_lock_password: (deviceId, password, deviceName) => credentialCall('save_lock', {
      device_id: String(deviceId || ''), password: String(password || ''), device_name: String(deviceName || '')
    }),
    clear_lock_password: (deviceId) => credentialCall('clear_lock', {device_id: String(deviceId || '')}),
    try_auto_unlock: (deviceJson, token) => fetch('/api/native/try-auto-unlock', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({device: JSON.parse(deviceJson), token}),
      cache: 'no-store'
    }).then((response) => response.json())
  }};
  document.addEventListener('mousedown', (event) => {
    if (event.button !== 0 || !event.target.closest('.pywebview-drag-region') || event.target.closest('button,input')) return;
    call('drag');
    event.preventDefault();
  }, true);
  window.dispatchEvent(new CustomEvent('pywebviewready'));
})();";
    }
}
