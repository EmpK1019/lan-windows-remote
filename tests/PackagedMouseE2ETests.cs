using System;
using System.Drawing;
using System.Net;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

internal static class PackagedMouseE2ETests
{
    [StructLayout(LayoutKind.Sequential)]
    private struct PointNative { public int X; public int Y; }

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

    [DllImport("user32.dll")]
    private static extern bool GetCursorPos(out PointNative point);

    [DllImport("user32.dll")]
    private static extern bool SetCursorPos(int x, int y);

    [DllImport("user32.dll")]
    private static extern int GetSystemMetrics(int index);

    [DllImport("user32.dll")]
    private static extern bool SetProcessDPIAware();

    [STAThread]
    private static int Main(string[] args)
    {
        int port;
        if (args.Length != 2 || !Int32.TryParse(args[0], out port))
        {
            Console.Error.WriteLine("Usage: PackagedMouseE2ETests.exe <port> <token>");
            return 2;
        }

        string endpoint = "http://127.0.0.1:" + port + "/input";
        string token = args[1];
        int clicks = 0;
        int downs = 0;
        int ups = 0;
        string diagnostics = "";
        Exception failure = null;

        SetProcessDPIAware();
        Application.EnableVisualStyles();
        using (Form form = new Form())
        using (Button button = new Button())
        {
            form.Text = "LAN Remote packaged mouse audit";
            form.ClientSize = new Size(520, 260);
            form.StartPosition = FormStartPosition.CenterScreen;
            form.TopMost = true;
            button.Text = "Remote click target";
            button.Size = new Size(220, 80);
            button.Location = new Point(150, 90);
            button.Click += delegate { clicks += 1; };
            button.MouseDown += delegate { downs += 1; };
            button.MouseUp += delegate { ups += 1; };
            form.Controls.Add(button);

            form.Shown += delegate
            {
                ForceForeground(form);
                Task.Run(delegate
                {
                    PointNative original;
                    GetCursorPos(out original);
                    try
                    {
                        Thread.Sleep(700);
                        Point target = (Point)form.Invoke(new Func<Point>(delegate
                        {
                            ForceForeground(form);
                            return button.PointToScreen(new Point(button.Width / 2, button.Height / 2));
                        }));
                        int relativeX = target.X - GetSystemMetrics(76);
                        int relativeY = target.Y - GetSystemMetrics(77);
                        Send(endpoint, token, "mouse_move", relativeX, relativeY, 0);
                        PointNative observed;
                        GetCursorPos(out observed);
                        diagnostics = "target=" + target.X + "," + target.Y + " observed=" + observed.X + "," + observed.Y;
                        Send(endpoint, token, "mouse_down", relativeX, relativeY, 0);
                        Send(endpoint, token, "mouse_up", relativeX, relativeY, 0);
                        Thread.Sleep(500);
                    }
                    catch (Exception ex)
                    {
                        failure = ex;
                    }
                    finally
                    {
                        SetCursorPos(original.X, original.Y);
                        form.BeginInvoke(new Action(form.Close));
                    }
                });
            };
            Application.Run(form);
        }

        if (failure != null)
        {
            Console.Error.WriteLine(failure.ToString());
            return 1;
        }
        if (clicks != 1 || downs != 1 || ups != 1)
        {
            Console.Error.WriteLine(
                "Mouse click mismatch. Expected=1 Actual clicks=" + clicks + " downs=" + downs + " ups=" + ups + " " + diagnostics);
            return 1;
        }
        Console.WriteLine("PACKAGED_MOUSE_E2E_OK clicks=" + clicks + " " + diagnostics);
        return 0;
    }

    private static void ForceForeground(Form form)
    {
        IntPtr foreground = GetForegroundWindow();
        uint currentThread = GetCurrentThreadId();
        uint foregroundThread = GetWindowThreadProcessId(foreground, IntPtr.Zero);
        bool attached = foregroundThread != 0 && foregroundThread != currentThread &&
            AttachThreadInput(currentThread, foregroundThread, true);
        try
        {
            form.BringToFront();
            form.Activate();
            SetForegroundWindow(form.Handle);
        }
        finally
        {
            if (attached) AttachThreadInput(currentThread, foregroundThread, false);
        }
    }

    private static void Send(string endpoint, string token, string type, int x, int y, int button)
    {
        string payload = "{\"type\":\"" + type + "\",\"x\":" + x + ",\"y\":" + y +
            ",\"button\":" + button + ",\"monitor\":\"all\"}";
        using (WebClient client = new WebClient())
        {
            client.Encoding = Encoding.UTF8;
            client.Headers[HttpRequestHeader.ContentType] = "application/json";
            client.Headers["X-Remote-Token"] = token;
            client.UploadString(endpoint, "POST", payload);
        }
        Thread.Sleep(40);
    }
}
