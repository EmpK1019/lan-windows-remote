using System;
using System.Drawing;
using System.Net;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

internal static class PackagedKeyboardE2ETests
{
    private const string Expected = "Hello LAN 123! 中文";
    private const string ExpectedPhysical = "hellolan123";

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
    private static extern IntPtr SetFocus(IntPtr window);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr LoadKeyboardLayout(string id, uint flags);

    [DllImport("user32.dll")]
    private static extern IntPtr ActivateKeyboardLayout(IntPtr layout, uint flags);

    [DllImport("imm32.dll")]
    private static extern IntPtr ImmAssociateContext(IntPtr window, IntPtr context);

    [STAThread]
    private static int Main(string[] args)
    {
        int port;
        if (args.Length != 2 || !Int32.TryParse(args[0], out port))
        {
            Console.Error.WriteLine("Usage: PackagedKeyboardE2ETests.exe <port> <token>");
            return 2;
        }

        string endpoint = "http://127.0.0.1:" + port + "/input";
        string token = args[1];
        string result = null;
        Exception failure = null;

        Application.EnableVisualStyles();
        using (Form form = new Form())
        using (TextBox textBox = new TextBox())
        {
            form.Text = "LAN Remote packaged keyboard audit";
            form.ClientSize = new Size(520, 100);
            form.StartPosition = FormStartPosition.CenterScreen;
            form.TopMost = true;
            form.ShowInTaskbar = true;
            textBox.Font = new Font("Segoe UI", 16);
            textBox.Dock = DockStyle.Fill;
            textBox.Multiline = true;
            form.Controls.Add(textBox);

            form.Shown += delegate
            {
                ForceForeground(form, textBox);
                Task.Run(delegate
                {
                    try
                    {
                        Thread.Sleep(700);
                        form.Invoke(new Action(delegate
                        {
                            form.TopMost = true;
                            ForceForeground(form, textBox);
                        }));
                        Thread.Sleep(250);
                        TypeAscii(endpoint, token);
                        Thread.Sleep(350);
                        string physical = (string)form.Invoke(new Func<string>(delegate { return textBox.Text; }));
                        if (!String.Equals(physical, ExpectedPhysical, StringComparison.Ordinal))
                        {
                            throw new InvalidOperationException(
                                "Physical key mismatch. Expected=[" + ExpectedPhysical + "] Actual=[" + physical + "]");
                        }
                        Key(endpoint, token, "Control", "ControlLeft", true);
                        KeyPress(endpoint, token, "a", "KeyA");
                        Key(endpoint, token, "Control", "ControlLeft", false);
                        KeyPress(endpoint, token, "Backspace", "Backspace");
                        Send(endpoint, token, "{\"type\":\"text\",\"text\":\"Hello LAN 123! 中文\"}");
                        Thread.Sleep(700);
                        result = (string)form.Invoke(new Func<string>(delegate { return textBox.Text; }));
                    }
                    catch (Exception ex)
                    {
                        failure = ex;
                    }
                    finally
                    {
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
        if (!String.Equals(result, Expected, StringComparison.Ordinal))
        {
            Console.Error.WriteLine("Keyboard text mismatch. Expected=[" + Expected + "] Actual=[" + result + "]");
            return 1;
        }
        Console.WriteLine("PACKAGED_KEYBOARD_E2E_OK " + result);
        return 0;
    }

    private static void TypeAscii(string endpoint, string token)
    {
        foreach (char character in "hellolan")
        {
            KeyPress(endpoint, token, character.ToString(), "Key" + Char.ToUpperInvariant(character));
        }
        KeyPress(endpoint, token, "1", "Digit1");
        KeyPress(endpoint, token, "2", "Digit2");
        KeyPress(endpoint, token, "3", "Digit3");
    }

    private static void ForceForeground(Form form, TextBox textBox)
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
            textBox.Select();
            textBox.Focus();
            SetFocus(textBox.Handle);
            ImmAssociateContext(textBox.Handle, IntPtr.Zero);
            IntPtr englishLayout = LoadKeyboardLayout("00000409", 1);
            if (englishLayout != IntPtr.Zero) ActivateKeyboardLayout(englishLayout, 0);
        }
        finally
        {
            if (attached) AttachThreadInput(currentThread, foregroundThread, false);
        }
    }

    private static void KeyPress(string endpoint, string token, string key, string code)
    {
        Key(endpoint, token, key, code, true);
        Key(endpoint, token, key, code, false);
    }

    private static void Key(string endpoint, string token, string key, string code, bool down)
    {
        string type = down ? "key_down" : "key_up";
        Send(endpoint, token, "{\"type\":\"" + type + "\",\"key\":\"" + key + "\",\"code\":\"" + code + "\"}");
    }

    private static void Send(string endpoint, string token, string payload)
    {
        using (WebClient client = new WebClient())
        {
            client.Encoding = Encoding.UTF8;
            client.Headers[HttpRequestHeader.ContentType] = "application/json";
            client.Headers["X-Remote-Token"] = token;
            client.UploadString(endpoint, "POST", payload);
        }
        Thread.Sleep(25);
    }
}
