using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
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

        [DllImport("user32.dll")]
        private static extern bool ReleaseCapture();

        [DllImport("user32.dll")]
        private static extern IntPtr SendMessage(IntPtr window, int message, IntPtr wParam, IntPtr lParam);

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

            Shown += async delegate
            {
                try
                {
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
