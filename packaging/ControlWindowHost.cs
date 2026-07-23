using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
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

    internal enum GlassIcon
    {
        None,
        Chevron,
        Grip,
        Monitor,
        Keyboard,
        Clipboard,
        Folder,
        Lock,
        Unlock,
        Fullscreen,
        Minimize,
        Maximize,
        Close
    }

    internal sealed class GlassToolButton : Control
    {
        private bool hovered;
        private bool pressed;
        private bool active;
        private GlassIcon icon;
        private string caption = String.Empty;

        public GlassToolButton()
        {
            SetStyle(
                ControlStyles.AllPaintingInWmPaint |
                ControlStyles.OptimizedDoubleBuffer |
                ControlStyles.ResizeRedraw |
                ControlStyles.SupportsTransparentBackColor |
                ControlStyles.UserPaint,
                true);
            BackColor = Color.Transparent;
            ForeColor = Color.FromArgb(242, 246, 250);
            Font = new Font("Segoe UI", 8.5f, FontStyle.Bold, GraphicsUnit.Point);
            Cursor = Cursors.Hand;
            TabStop = false;
            Size = new Size(32, 31);
        }

        public GlassIcon IconKind
        {
            get { return icon; }
            set { icon = value; Invalidate(); }
        }

        public string Caption
        {
            get { return caption; }
            set { caption = value ?? String.Empty; Invalidate(); }
        }

        public bool Active
        {
            get { return active; }
            set { active = value; Invalidate(); }
        }

        protected override void OnMouseEnter(EventArgs e)
        {
            hovered = true;
            Invalidate();
            base.OnMouseEnter(e);
        }

        protected override void OnMouseLeave(EventArgs e)
        {
            hovered = false;
            pressed = false;
            Invalidate();
            base.OnMouseLeave(e);
        }

        protected override void OnMouseDown(MouseEventArgs e)
        {
            if (e.Button == MouseButtons.Left)
            {
                pressed = true;
                Invalidate();
            }
            base.OnMouseDown(e);
        }

        protected override void OnMouseUp(MouseEventArgs e)
        {
            pressed = false;
            Invalidate();
            base.OnMouseUp(e);
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            Rectangle bounds = new Rectangle(1, 1, Math.Max(1, Width - 2), Math.Max(1, Height - 2));
            if (hovered || pressed)
            {
                Color fill = active
                    ? Color.FromArgb(pressed ? 74 : 48, 45, 151, 113)
                    : Color.FromArgb(pressed ? 74 : 48, 255, 255, 255);
                using (GraphicsPath background = RoundedRectangle(bounds, 8))
                using (SolidBrush brush = new SolidBrush(fill))
                {
                    e.Graphics.FillPath(brush, background);
                }
            }

            Color color = Enabled
                ? (active ? Color.FromArgb(164, 244, 209) : ForeColor)
                : Color.FromArgb(120, 225, 230, 236);
            if (!String.IsNullOrEmpty(caption))
            {
                Rectangle textBounds = bounds;
                if (icon != GlassIcon.None)
                {
                    textBounds.X += 23;
                    textBounds.Width -= 25;
                    DrawIcon(e.Graphics, icon, new Rectangle(bounds.X + 6, bounds.Y + 6, 18, 18), color);
                }
                e.Graphics.TextRenderingHint = System.Drawing.Text.TextRenderingHint.AntiAliasGridFit;
                using (SolidBrush textBrush = new SolidBrush(color))
                using (StringFormat format = new StringFormat())
                {
                    format.Alignment = StringAlignment.Center;
                    format.LineAlignment = StringAlignment.Center;
                    format.FormatFlags = StringFormatFlags.NoWrap;
                    format.Trimming = StringTrimming.None;
                    SizeF measured = e.Graphics.MeasureString(caption, Font, Int32.MaxValue, format);
                    float horizontalScale = measured.Width > textBounds.Width && measured.Width > 0
                        ? Math.Max(0.72f, textBounds.Width / measured.Width)
                        : 1.0f;
                    GraphicsState textState = e.Graphics.Save();
                    if (horizontalScale < 1.0f)
                    {
                        float centerX = textBounds.Left + textBounds.Width / 2.0f;
                        e.Graphics.TranslateTransform(centerX, 0);
                        e.Graphics.ScaleTransform(horizontalScale, 1.0f);
                        e.Graphics.TranslateTransform(-centerX, 0);
                        float expandedWidth = textBounds.Width / horizontalScale;
                        textBounds = new Rectangle(
                            (int)Math.Round(centerX - expandedWidth / 2.0f),
                            textBounds.Top,
                            (int)Math.Ceiling(expandedWidth),
                            textBounds.Height);
                    }
                    e.Graphics.DrawString(caption, Font, textBrush, textBounds, format);
                    e.Graphics.Restore(textState);
                }
            }
            else
            {
                DrawIcon(e.Graphics, icon, bounds, color);
            }
        }

        private static void DrawIcon(Graphics graphics, GlassIcon value, Rectangle bounds, Color color)
        {
            float centerX = bounds.Left + bounds.Width / 2.0f;
            float centerY = bounds.Top + bounds.Height / 2.0f;
            GraphicsState saved = graphics.Save();
            graphics.TranslateTransform(centerX, centerY);
            graphics.ScaleTransform(1.16f, 1.16f);
            graphics.TranslateTransform(-centerX, -centerY);
            using (Pen pen = new Pen(color, 1.7f))
            {
                pen.StartCap = LineCap.Round;
                pen.EndCap = LineCap.Round;
                switch (value)
                {
                    case GlassIcon.Chevron:
                        graphics.DrawLines(pen, new[] {
                            new PointF(centerX - 5, centerY - 2),
                            new PointF(centerX, centerY + 3),
                            new PointF(centerX + 5, centerY - 2)
                        });
                        break;
                    case GlassIcon.Grip:
                        using (SolidBrush dot = new SolidBrush(color))
                        {
                            for (int y = -5; y <= 5; y += 5)
                            {
                                graphics.FillEllipse(dot, centerX - 1, centerY + y - 1, 2, 2);
                            }
                        }
                        break;
                    case GlassIcon.Monitor:
                        graphics.DrawRectangle(pen, centerX - 7, centerY - 6, 14, 10);
                        graphics.DrawLine(pen, centerX, centerY + 4, centerX, centerY + 7);
                        graphics.DrawLine(pen, centerX - 4, centerY + 7, centerX + 4, centerY + 7);
                        break;
                    case GlassIcon.Keyboard:
                        graphics.DrawRectangle(pen, centerX - 8, centerY - 5, 16, 10);
                        for (int x = -5; x <= 5; x += 5)
                        {
                            graphics.DrawLine(pen, centerX + x, centerY - 2, centerX + x + 1, centerY - 2);
                        }
                        graphics.DrawLine(pen, centerX - 4, centerY + 2, centerX + 4, centerY + 2);
                        break;
                    case GlassIcon.Clipboard:
                        graphics.DrawRectangle(pen, centerX - 6, centerY - 6, 12, 13);
                        graphics.DrawRectangle(pen, centerX - 3, centerY - 8, 6, 3);
                        graphics.DrawLine(pen, centerX - 3, centerY - 1, centerX + 3, centerY - 1);
                        graphics.DrawLine(pen, centerX - 3, centerY + 3, centerX + 2, centerY + 3);
                        break;
                    case GlassIcon.Folder:
                        PointF[] folder = {
                            new PointF(centerX - 8, centerY - 5),
                            new PointF(centerX - 2, centerY - 5),
                            new PointF(centerX, centerY - 2),
                            new PointF(centerX + 8, centerY - 2),
                            new PointF(centerX + 7, centerY + 6),
                            new PointF(centerX - 8, centerY + 6)
                        };
                        graphics.DrawPolygon(pen, folder);
                        break;
                    case GlassIcon.Lock:
                        graphics.DrawRectangle(pen, centerX - 6, centerY - 1, 12, 9);
                        graphics.DrawArc(pen, centerX - 4, centerY - 7, 8, 10, 180, -180);
                        break;
                    case GlassIcon.Unlock:
                        graphics.DrawRectangle(pen, centerX - 6, centerY - 1, 12, 9);
                        graphics.DrawArc(pen, centerX - 1, centerY - 7, 8, 10, 180, -150);
                        break;
                    case GlassIcon.Fullscreen:
                        graphics.DrawLines(pen, new[] { new PointF(centerX - 2, centerY - 7), new PointF(centerX - 7, centerY - 7), new PointF(centerX - 7, centerY - 2) });
                        graphics.DrawLines(pen, new[] { new PointF(centerX + 2, centerY - 7), new PointF(centerX + 7, centerY - 7), new PointF(centerX + 7, centerY - 2) });
                        graphics.DrawLines(pen, new[] { new PointF(centerX - 7, centerY + 2), new PointF(centerX - 7, centerY + 7), new PointF(centerX - 2, centerY + 7) });
                        graphics.DrawLines(pen, new[] { new PointF(centerX + 7, centerY + 2), new PointF(centerX + 7, centerY + 7), new PointF(centerX + 2, centerY + 7) });
                        break;
                    case GlassIcon.Minimize:
                        graphics.DrawLine(pen, centerX - 6, centerY + 4, centerX + 6, centerY + 4);
                        break;
                    case GlassIcon.Maximize:
                        graphics.DrawRectangle(pen, centerX - 6, centerY - 6, 12, 12);
                        break;
                    case GlassIcon.Close:
                        graphics.DrawLine(pen, centerX - 5, centerY - 5, centerX + 5, centerY + 5);
                        graphics.DrawLine(pen, centerX + 5, centerY - 5, centerX - 5, centerY + 5);
                        break;
                }
            }
            graphics.Restore(saved);
        }

        private static GraphicsPath RoundedRectangle(Rectangle bounds, int radius)
        {
            GraphicsPath path = new GraphicsPath();
            int diameter = Math.Max(2, radius * 2);
            Rectangle arc = new Rectangle(bounds.Left, bounds.Top, diameter, diameter);
            path.AddArc(arc, 180, 90);
            arc.X = bounds.Right - diameter;
            path.AddArc(arc, 270, 90);
            arc.Y = bounds.Bottom - diameter;
            path.AddArc(arc, 0, 90);
            arc.X = bounds.Left;
            path.AddArc(arc, 90, 90);
            path.CloseFigure();
            return path;
        }
    }

    internal sealed class NativeGlassToolbar : Form
    {
        private const int WsExToolWindow = 0x00000080;
        private const int WsExNoActivate = 0x08000000;
        private const int WmNcLButtonDown = 0x00A1;
        private const int HtCaption = 2;
        private const int TopDockThreshold = 14;
        private readonly Action<string, string> action;
        private readonly ToolTip tips = new ToolTip();
        private readonly List<GlassToolButton> expandedButtons = new List<GlassToolButton>();
        private readonly List<KeyValuePair<string, string>> monitors = new List<KeyValuePair<string, string>>();
        private readonly GlassToolButton grip;
        private readonly GlassToolButton monitor;
        private readonly GlassToolButton fps;
        private readonly GlassToolButton keyboard;
        private readonly GlassToolButton clipboard;
        private readonly GlassToolButton files;
        private readonly GlassToolButton remoteLock;
        private readonly GlassToolButton fullscreen;
        private readonly GlassToolButton status;
        private readonly GlassToolButton minimize;
        private readonly GlassToolButton maximize;
        private readonly GlassToolButton close;
        private readonly GlassToolButton collapse;
        private ContextMenuStrip activeMenu;
        private Control activeMenuAnchor;
        private bool collapsed;
        private bool viewOnly;
        private bool userPositioned;
        private bool dockedAtTop = true;
        private Point ownerOffset;
        private Point lastOwnerOrigin;
        private bool ownerPositionKnown;

        [DllImport("user32.dll")]
        private static extern bool ReleaseCapture();

        [DllImport("user32.dll")]
        private static extern IntPtr SendMessage(IntPtr window, int message, IntPtr wParam, IntPtr lParam);

        public NativeGlassToolbar(Action<string, string> actionCallback)
        {
            action = actionCallback;
            AutoScaleMode = AutoScaleMode.None;
            BackColor = Color.FromArgb(53, 56, 63);
            DoubleBuffered = true;
            FormBorderStyle = FormBorderStyle.None;
            Opacity = 0.40;
            ShowInTaskbar = false;
            StartPosition = FormStartPosition.Manual;
            TopMost = false;

            status = AddButton(GlassIcon.None, "●", "远程画面已连接 · 点击收起工具栏", "toolbar_toggle", 22);
            status.Cursor = Cursors.Hand;
            grip = AddButton(GlassIcon.Grip, String.Empty, "拖动工具栏", "noop", 10);
            grip.Cursor = Cursors.SizeAll;
            grip.MouseDown += BeginToolbarDrag;
            monitor = AddButton(GlassIcon.Monitor, String.Empty, "选择远端显示器", "monitor_menu", 30);
            fps = AddButton(GlassIcon.None, "60 FPS", "帧率上限", "fps_menu", 58);
            fps.Font = new Font("Noto Sans SC", 11.25f, FontStyle.Regular, GraphicsUnit.Point);
            keyboard = AddButton(GlassIcon.Keyboard, String.Empty, "键盘控制", "keyboard", 30);
            clipboard = AddButton(GlassIcon.Clipboard, String.Empty, "剪贴板同步", "clipboard", 30);
            files = AddButton(GlassIcon.Folder, String.Empty, "远程文件", "files", 30);
            remoteLock = AddButton(GlassIcon.Unlock, String.Empty, "锁定远端电脑", "lock", 30);
            fullscreen = AddButton(GlassIcon.Fullscreen, String.Empty, "全屏", "fullscreen", 30);
            minimize = AddButton(GlassIcon.Minimize, String.Empty, "最小化", "minimize", 32);
            maximize = AddButton(GlassIcon.Maximize, String.Empty, "最大化/还原", "maximize", 32);
            close = AddButton(GlassIcon.Close, String.Empty, "断开并关闭", "close", 32);
            collapse = AddButton(GlassIcon.Chevron, String.Empty, "展开工具栏", "toolbar_toggle", 32);
            expandedButtons.AddRange(new[] {
                status, grip, monitor, fps, keyboard, clipboard, files, remoteLock,
                fullscreen
            });
            minimize.Visible = false;
            maximize.Visible = false;
            close.Visible = false;
            LayoutButtons();
        }

        public bool UserPositioned { get { return userPositioned; } }

        public bool DockedAtTop { get { return dockedAtTop; } }

        public void PositionForOwner(Rectangle ownerBounds, Point defaultLocation)
        {
            if (ownerBounds.Width < 1 || ownerBounds.Height < 1) return;
            if (!userPositioned)
            {
                dockedAtTop = defaultLocation.Y <= ownerBounds.Top + 40 + TopDockThreshold;
                if (dockedAtTop) defaultLocation.Y = DockedTop(ownerBounds);
                Location = ClampLocation(ownerBounds, defaultLocation);
            }
            else
            {
                Point requested = ownerPositionKnown && lastOwnerOrigin != ownerBounds.Location
                    ? new Point(ownerBounds.Left + ownerOffset.X, ownerBounds.Top + ownerOffset.Y)
                    : Location;
                if (dockedAtTop) requested.Y = DockedTop(ownerBounds);
                Location = ClampLocation(ownerBounds, requested);
            }
            RememberOwnerPosition(ownerBounds);
        }

        protected override bool ShowWithoutActivation { get { return true; } }

        protected override CreateParams CreateParams
        {
            get
            {
                CreateParams value = base.CreateParams;
                value.ExStyle |= WsExToolWindow | WsExNoActivate;
                return value;
            }
        }

        public void UpdateState(Dictionary<string, object> values)
        {
            if (values == null) return;
            int centerX = Left + Width / 2;
            collapsed = ReadBoolean(values, "collapsed", collapsed);
            viewOnly = ReadBoolean(values, "view_only", viewOnly);
            fps.Caption = ReadInteger(values, "fps", 60).ToString() + " FPS";
            keyboard.Active = ReadBoolean(values, "keyboard", false);
            clipboard.Active = ReadBoolean(values, "clipboard", false);
            fullscreen.Active = ReadBoolean(values, "fullscreen", false);
            bool remoteLocked = ReadBoolean(values, "unlock_visible", false);
            remoteLock.IconKind = remoteLocked ? GlassIcon.Lock : GlassIcon.Unlock;
            remoteLock.Active = remoteLocked;
            tips.SetToolTip(remoteLock, remoteLocked ? "解锁被控电脑" : "锁定远端电脑");
            remoteLock.AccessibleName = remoteLocked ? "解锁被控电脑" : "锁定远端电脑";
            bool statusError = ReadBoolean(values, "status_error", false);
            status.ForeColor = statusError ? Color.FromArgb(255, 118, 132) : Color.FromArgb(91, 230, 172);
            tips.SetToolTip(
                status,
                (statusError ? "远程画面异常" : "远程画面已连接") +
                (collapsed ? " · 点击展开工具栏" : " · 点击收起工具栏"));
            tips.SetToolTip(collapse, "展开工具栏");
            ReadMonitors(values);
            LayoutButtons();
            if (userPositioned)
            {
                Left = centerX - Width / 2;
                Rectangle ownerBounds = OwnerClientBounds();
                if (ownerBounds.Width > 0 && ownerBounds.Height > 0)
                {
                    Point requested = new Point(centerX - Width / 2, Top);
                    if (dockedAtTop) requested.Y = DockedTop(ownerBounds);
                    Location = ClampLocation(ownerBounds, requested);
                    RememberOwnerPosition(ownerBounds);
                }
            }
        }

        protected override void OnPaintBackground(PaintEventArgs e)
        {
            Rectangle bounds = ClientRectangle;
            if (bounds.Width < 1 || bounds.Height < 1) return;
            using (LinearGradientBrush brush = new LinearGradientBrush(
                bounds,
                Color.FromArgb(105, 109, 117),
                Color.FromArgb(35, 38, 44),
                LinearGradientMode.Vertical))
            {
                e.Graphics.FillRectangle(brush, bounds);
            }
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            Rectangle border = new Rectangle(0, 0, Math.Max(1, Width - 1), Math.Max(1, Height - 1));
            using (GraphicsPath path = RoundedRectangle(border, collapsed ? 13 : 16))
            using (Pen pen = new Pen(Color.FromArgb(136, 238, 244, 249), 1.0f))
            {
                e.Graphics.DrawPath(pen, path);
            }
            using (Pen highlight = new Pen(Color.FromArgb(36, 255, 255, 255), 1.0f))
            {
                e.Graphics.DrawLine(highlight, 8, 1, Math.Max(8, Width - 9), 1);
            }
            base.OnPaint(e);
        }

        protected override void OnSizeChanged(EventArgs e)
        {
            base.OnSizeChanged(e);
            if (Width < 1 || Height < 1) return;
            using (GraphicsPath path = RoundedRectangle(
                new Rectangle(0, 0, Width, Height),
                collapsed ? 13 : 16))
            {
                Region = new Region(path);
            }
            PositionActiveMenu();
        }

        protected override void OnLocationChanged(EventArgs e)
        {
            base.OnLocationChanged(e);
            PositionActiveMenu();
        }

        private GlassToolButton AddButton(
            GlassIcon icon,
            string caption,
            string tooltip,
            string actionName,
            int width)
        {
            GlassToolButton button = new GlassToolButton
            {
                IconKind = icon,
                Caption = caption,
                Width = width,
                Height = 31,
                AccessibleName = tooltip
            };
            tips.SetToolTip(button, tooltip);
            button.Click += delegate
            {
                if (String.Equals(actionName, "monitor_menu", StringComparison.Ordinal)) ShowMonitorMenu(button);
                else if (String.Equals(actionName, "fps_menu", StringComparison.Ordinal)) ShowChoiceMenu(
                    button,
                    new[] { new KeyValuePair<string, string>("30", "30 FPS"), new KeyValuePair<string, string>("60", "60 FPS"), new KeyValuePair<string, string>("120", "120 FPS") },
                    "fps");
                else if (String.Equals(actionName, "scale_menu", StringComparison.Ordinal)) ShowChoiceMenu(
                    button,
                    new[] { new KeyValuePair<string, string>("fit", "适应 · 完整画面"), new KeyValuePair<string, string>("fill", "填充 · 无黑边") },
                    "scale_mode");
                else if (String.Equals(actionName, "noop", StringComparison.Ordinal)) return;
                else action(actionName, String.Empty);
            };
            Controls.Add(button);
            return button;
        }

        private void BeginToolbarDrag(object sender, MouseEventArgs e)
        {
            if (e.Button != MouseButtons.Left || !IsHandleCreated) return;
            ReleaseCapture();
            SendMessage(Handle, WmNcLButtonDown, new IntPtr(HtCaption), IntPtr.Zero);
            Rectangle ownerBounds = OwnerClientBounds();
            if (ownerBounds.Width < 1 || ownerBounds.Height < 1) return;
            ApplyUserLocation(ownerBounds, Location);
        }

        private void ApplyUserLocation(Rectangle ownerBounds, Point requested)
        {
            int toolbarTopEdge = ownerBounds.Top + 40;
            bool nextDocked = requested.Y <= toolbarTopEdge + TopDockThreshold;
            if (nextDocked) requested.Y = ownerBounds.Top + 48;
            Location = ClampLocation(ownerBounds, requested);
            userPositioned = true;
            dockedAtTop = nextDocked;
            RememberOwnerPosition(ownerBounds);
            action("toolbar_docked", dockedAtTop ? "1" : "0");
        }

        private Rectangle OwnerClientBounds()
        {
            Form owner = Owner;
            if (owner == null || owner.IsDisposed || !owner.IsHandleCreated) return Rectangle.Empty;
            return new Rectangle(owner.PointToScreen(Point.Empty), owner.ClientSize);
        }

        private void RememberOwnerPosition(Rectangle ownerBounds)
        {
            lastOwnerOrigin = ownerBounds.Location;
            ownerOffset = new Point(Left - ownerBounds.Left, Top - ownerBounds.Top);
            ownerPositionKnown = true;
        }

        private Point ClampLocation(Rectangle ownerBounds, Point requested)
        {
            int minimumY = collapsed && dockedAtTop ? ownerBounds.Top : ownerBounds.Top + 40;
            int maximumX = Math.Max(ownerBounds.Left, ownerBounds.Right - Width);
            int maximumY = Math.Max(minimumY, ownerBounds.Bottom - Height);
            return new Point(
                Math.Max(ownerBounds.Left, Math.Min(maximumX, requested.X)),
                Math.Max(minimumY, Math.Min(maximumY, requested.Y)));
        }

        private int DockedTop(Rectangle ownerBounds)
        {
            return collapsed ? ownerBounds.Top : ownerBounds.Top + 48;
        }

        private void LayoutButtons()
        {
            if (collapsed)
            {
                foreach (GlassToolButton button in expandedButtons) button.Visible = false;
                collapse.Visible = true;
                collapse.Bounds = new Rectangle(6, 1, 32, 22);
                ClientSize = new Size(44, 24);
                return;
            }

            grip.Visible = true;
            monitor.Visible = true;
            fps.Visible = true;
            keyboard.Visible = !viewOnly;
            clipboard.Visible = !viewOnly;
            files.Visible = !viewOnly;
            remoteLock.Visible = !viewOnly;
            fullscreen.Visible = true;
            minimize.Visible = false;
            maximize.Visible = false;
            close.Visible = false;
            collapse.Visible = false;

            int x = 6;
            foreach (GlassToolButton button in expandedButtons)
            {
                bool include = !viewOnly ||
                    (button != keyboard && button != clipboard && button != files && button != remoteLock);
                button.Visible = include;
                if (!include) continue;
                button.Location = new Point(x, 6);
                x += button.Width + 2;
            }
            x += 3;
            ClientSize = new Size(x, 43);
        }

        private void ReadMonitors(Dictionary<string, object> values)
        {
            object raw;
            if (!values.TryGetValue("monitors", out raw) || raw == null || raw is string) return;
            IEnumerable items = raw as IEnumerable;
            if (items == null) return;
            monitors.Clear();
            foreach (object item in items)
            {
                Dictionary<string, object> entry = item as Dictionary<string, object>;
                if (entry == null) continue;
                string id = ReadString(entry, "id");
                string label = ReadString(entry, "label");
                if (!String.IsNullOrWhiteSpace(id)) monitors.Add(new KeyValuePair<string, string>(id, String.IsNullOrWhiteSpace(label) ? id : label));
            }
        }

        private void ShowMonitorMenu(Control anchor)
        {
            List<KeyValuePair<string, string>> options = monitors.Count > 0
                ? new List<KeyValuePair<string, string>>(monitors)
                : new List<KeyValuePair<string, string>> { new KeyValuePair<string, string>("all", "全部显示器") };
            ShowChoiceMenu(anchor, options.ToArray(), "monitor");
        }

        private void ShowChoiceMenu(
            Control anchor,
            KeyValuePair<string, string>[] options,
            string actionName)
        {
            if (activeMenu != null && !activeMenu.IsDisposed) activeMenu.Close();
            ContextMenuStrip menu = new ContextMenuStrip
            {
                BackColor = Color.FromArgb(37, 39, 45),
                ForeColor = Color.FromArgb(239, 242, 246),
                ShowImageMargin = false,
                ShowCheckMargin = false,
                Font = new Font("Segoe UI", 9.0f, FontStyle.Regular, GraphicsUnit.Point)
            };
            foreach (KeyValuePair<string, string> option in options)
            {
                ToolStripMenuItem item = new ToolStripMenuItem(option.Value) { Tag = option.Key };
                item.Click += delegate(object sender, EventArgs args)
                {
                    ToolStripMenuItem selected = sender as ToolStripMenuItem;
                    action(actionName, Convert.ToString(selected == null ? null : selected.Tag));
                };
                menu.Items.Add(item);
            }
            activeMenu = menu;
            activeMenuAnchor = anchor;
            menu.Closed += delegate
            {
                if (ReferenceEquals(activeMenu, menu))
                {
                    activeMenu = null;
                    activeMenuAnchor = null;
                }
                menu.Dispose();
            };
            menu.Show(anchor, new Point(0, anchor.Height + 4));
            PositionActiveMenu();
        }

        private void PositionActiveMenu()
        {
            ContextMenuStrip menu = activeMenu;
            Control anchor = activeMenuAnchor;
            if (menu == null || menu.IsDisposed || !menu.Visible ||
                anchor == null || anchor.IsDisposed || !anchor.Visible) return;
            Point requested = anchor.PointToScreen(new Point(0, anchor.Height + 4));
            Rectangle workingArea = Screen.FromPoint(requested).WorkingArea;
            int left = Math.Max(
                workingArea.Left,
                Math.Min(Math.Max(workingArea.Left, workingArea.Right - menu.Width), requested.X));
            int top = requested.Y;
            if (top + menu.Height > workingArea.Bottom)
                top = Math.Max(workingArea.Top, anchor.PointToScreen(Point.Empty).Y - menu.Height - 4);
            menu.Location = new Point(left, top);
        }

        private static bool ReadBoolean(Dictionary<string, object> values, string key, bool fallback)
        {
            object value;
            if (!values.TryGetValue(key, out value) || value == null) return fallback;
            try { return Convert.ToBoolean(value); }
            catch { return fallback; }
        }

        private static int ReadInteger(Dictionary<string, object> values, string key, int fallback)
        {
            object value;
            if (!values.TryGetValue(key, out value) || value == null) return fallback;
            try { return Convert.ToInt32(value); }
            catch { return fallback; }
        }

        private static string ReadString(Dictionary<string, object> values, string key)
        {
            object value;
            return values.TryGetValue(key, out value) && value != null ? Convert.ToString(value) : String.Empty;
        }

        private static GraphicsPath RoundedRectangle(Rectangle bounds, int radius)
        {
            GraphicsPath path = new GraphicsPath();
            int diameter = Math.Max(2, radius * 2);
            Rectangle arc = new Rectangle(bounds.Left, bounds.Top, diameter, diameter);
            path.AddArc(arc, 180, 90);
            arc.X = bounds.Right - diameter;
            path.AddArc(arc, 270, 90);
            arc.Y = bounds.Bottom - diameter;
            path.AddArc(arc, 0, 90);
            arc.X = bounds.Left;
            path.AddArc(arc, 90, 90);
            path.CloseFigure();
            return path;
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
        private const int SwRestore = 9;
        private const uint SwpNoSize = 0x0001;
        private const uint SwpNoMove = 0x0002;
        private const uint SwpShowWindow = 0x0040;
        private const uint LlkhfExtended = 0x00000001;
        private const uint LlkhfInjected = 0x00000010;
        private const uint LlmhfInjected = 0x00000001;
        private const ulong RemoteInputExtraInfo = 0x4C414E52;
        private const int DwmWindowCornerPreference = 33;
        private const int DwmCornerDoNotRound = 1;
        private const int DwmCornerRound = 2;
        private static readonly IntPtr HwndTopMost = new IntPtr(-1);
        private static readonly IntPtr HwndNotTopMost = new IntPtr(-2);

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
        private IntPtr nativeVideoHandle = IntPtr.Zero;
        private string nativeVideoUnavailableReason = "native video DLL unavailable";
        private NativeGlassToolbar nativeToolbar;
        private bool nativeOverlayRequestedVisible;

        [DllImport("WindowsLANRemoteVideo.dll", CallingConvention = CallingConvention.StdCall)]
        private static extern IntPtr LANRemoteVideoCreate(IntPtr parent);

        [DllImport("WindowsLANRemoteVideo.dll", CharSet = CharSet.Unicode, CallingConvention = CallingConvention.StdCall)]
        private static extern int LANRemoteVideoConfigure(
            IntPtr handle,
            string host,
            uint port,
            string token,
            string monitor,
            uint fps,
            int left,
            int top,
            int width,
            int height);

        [DllImport("WindowsLANRemoteVideo.dll", CallingConvention = CallingConvention.StdCall)]
        private static extern void LANRemoteVideoSetLayout(
            IntPtr handle,
            int left,
            int top,
            int width,
            int height,
            int visible);

        [DllImport("WindowsLANRemoteVideo.dll", CallingConvention = CallingConvention.StdCall)]
        private static extern void LANRemoteVideoSetExclusions(
            IntPtr handle,
            [In] int[] rectangles,
            int count);

        [DllImport("WindowsLANRemoteVideo.dll", CallingConvention = CallingConvention.StdCall)]
        private static extern void LANRemoteVideoSetScaleMode(IntPtr handle, int fill);

        [DllImport("WindowsLANRemoteVideo.dll", CallingConvention = CallingConvention.StdCall)]
        private static extern void LANRemoteVideoSetCursor(
            IntPtr handle,
            int x,
            int y,
            int visible,
            int remoteWidth,
            int remoteHeight,
            int remoteOwner);

        [DllImport("WindowsLANRemoteVideo.dll", CharSet = CharSet.Unicode, CallingConvention = CallingConvention.StdCall)]
        private static extern int LANRemoteVideoGetStatus(IntPtr handle, StringBuilder output, int capacity);

        [DllImport("WindowsLANRemoteVideo.dll", CharSet = CharSet.Unicode, CallingConvention = CallingConvention.StdCall)]
        private static extern int LANRemoteVideoGetLastError(StringBuilder output, int capacity);

        [DllImport("WindowsLANRemoteVideo.dll", CallingConvention = CallingConvention.StdCall)]
        private static extern void LANRemoteVideoStop(IntPtr handle);

        [DllImport("WindowsLANRemoteVideo.dll", CallingConvention = CallingConvention.StdCall)]
        private static extern void LANRemoteVideoDestroy(IntPtr handle);

        [DllImport("user32.dll")]
        private static extern bool ReleaseCapture();

        [DllImport("user32.dll")]
        private static extern IntPtr SendMessage(IntPtr window, int message, IntPtr wParam, IntPtr lParam);

        [DllImport("dwmapi.dll")]
        private static extern int DwmSetWindowAttribute(
            IntPtr window,
            int attribute,
            ref int value,
            int valueSize);

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

        [DllImport("user32.dll")]
        private static extern bool SetForegroundWindow(IntPtr window);

        [DllImport("user32.dll")]
        private static extern bool ShowWindowAsync(IntPtr window, int command);

        [DllImport("user32.dll", SetLastError = true)]
        private static extern bool SetWindowPos(
            IntPtr window,
            IntPtr insertAfter,
            int x,
            int y,
            int width,
            int height,
            uint flags);

        [DllImport("user32.dll")]
        private static extern bool AllowSetForegroundWindow(uint processId);

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
            public bool FillMode;
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
            MinimumSize = remoteWindow ? new Size(720, 480) : new Size(820, 600);
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
                nativeToolbar = new NativeGlassToolbar(SendNativeOverlayAction);
                Deactivate += delegate { ReleaseNativePressedInputs(); };
            }

            Shown += async delegate
            {
                try
                {
                    if (remoteWindow)
                    {
                        PromoteInitialRemoteWindow();
                        InitializeKeyboardHook();
                        InitializeMouseHook();
                        StartNativeInputWorker();
                        try
                        {
                            nativeVideoHandle = LANRemoteVideoCreate(Handle);
                            if (nativeVideoHandle == IntPtr.Zero)
                            {
                                StringBuilder error = new StringBuilder(1024);
                                LANRemoteVideoGetLastError(error, error.Capacity);
                                if (error.Length > 0)
                                {
                                    nativeVideoUnavailableReason = error.ToString();
                                }
                            }
                        }
                        catch (DllNotFoundException ex)
                        {
                            nativeVideoHandle = IntPtr.Zero;
                            nativeVideoUnavailableReason = ex.Message;
                        }
                        catch (EntryPointNotFoundException ex)
                        {
                            nativeVideoHandle = IntPtr.Zero;
                            nativeVideoUnavailableReason = ex.Message;
                        }
                        catch (BadImageFormatException ex)
                        {
                            nativeVideoHandle = IntPtr.Zero;
                            nativeVideoUnavailableReason = ex.Message;
                        }
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

        protected override void OnHandleCreated(EventArgs e)
        {
            base.OnHandleCreated(e);
            ApplyWindowCornerPreference();
        }

        private void ApplyWindowCornerPreference()
        {
            if (!IsHandleCreated) return;
            int preference = fullscreen || WindowState == FormWindowState.Maximized
                ? DwmCornerDoNotRound
                : DwmCornerRound;
            try
            {
                DwmSetWindowAttribute(
                    Handle,
                    DwmWindowCornerPreference,
                    ref preference,
                    sizeof(int));
            }
            catch (DllNotFoundException)
            {
                // Windows versions without DWM corner preferences retain their native shape.
            }
        }

        private async void PromoteInitialRemoteWindow()
        {
            if (!remoteWindow || IsDisposed)
            {
                return;
            }
            // Keep the new controller above the main window until the
            // foreground handoff finishes, then release this lease.
            TopMost = true;
            ShowWindowAsync(Handle, SwRestore);
            BringToFront();
            Activate();
            SetForegroundWindow(Handle);
            await Task.Delay(900);
            if (!IsDisposed && !fullscreen)
            {
                TopMost = false;
            }
        }

        private async Task<bool> ActivateRemoteWindow(int processId)
        {
            if (remoteWindow || processId <= 0)
            {
                return false;
            }
            AllowSetForegroundWindow((uint)processId);
            for (int attempt = 0; attempt < 80; attempt++)
            {
                try
                {
                    using (Process process = Process.GetProcessById(processId))
                    {
                        if (process.HasExited)
                        {
                            return false;
                        }
                        process.Refresh();
                        IntPtr window = process.MainWindowHandle;
                        if (window != IntPtr.Zero)
                        {
                            ShowWindowAsync(window, SwRestore);
                            SetWindowPos(
                                window,
                                HwndTopMost,
                                0,
                                0,
                                0,
                                0,
                                SwpNoMove | SwpNoSize | SwpShowWindow);
                            bool activated = SetForegroundWindow(window);
                            SetWindowPos(
                                window,
                                HwndNotTopMost,
                                0,
                                0,
                                0,
                                0,
                                SwpNoMove | SwpNoSize | SwpShowWindow);
                            return activated || GetForegroundWindow() == window;
                        }
                    }
                }
                catch (ArgumentException)
                {
                    return false;
                }
                catch (InvalidOperationException)
                {
                    return false;
                }
                await Task.Delay(50);
            }
            return false;
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
            if (ShouldPassInjectedMouseInput(input.Flags, input.ExtraInfo))
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

        private bool PointInExclusion(NativeInputSession session, int x, int y)
        {
            Point point = new Point(x, y);
            if (nativeToolbar != null && nativeToolbar.Visible && nativeToolbar.Bounds.Contains(point))
            {
                return true;
            }
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
            double localX = screenX - session.RemoteBounds.Left;
            double localY = screenY - session.RemoteBounds.Top;
            double normalizedX = localX / session.RemoteBounds.Width;
            double normalizedY = localY / session.RemoteBounds.Height;
            remoteX = (int)Math.Round(normalizedX * Math.Max(0, session.RemoteWidth - 1));
            remoteY = (int)Math.Round(normalizedY * Math.Max(0, session.RemoteHeight - 1));
            remoteX = Math.Max(0, Math.Min(Math.Max(0, session.RemoteWidth - 1), remoteX));
            remoteY = Math.Max(0, Math.Min(Math.Max(0, session.RemoteHeight - 1), remoteY));
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

        private static bool ShouldPassInjectedMouseInput(uint flags, UIntPtr extraInfo)
        {
            // Ignore only LAN Remote's own synthetic input. Precision touchpads
            // and vendor mouse utilities can mark real user gestures as injected,
            // so those events must still travel through the native remote path.
            return
                (flags & LlmhfInjected) != 0 &&
                extraInfo.ToUInt64() == RemoteInputExtraInfo;
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
                Suspended = ReadBoolean(values, "suspended", false),
                FillMode = String.Equals(ReadString(values, "scale_mode"), "fill", StringComparison.Ordinal)
            };
            lock (nativeInputLock)
            {
                nativeInputSession = session;
                pendingNativeMouseMove = null;
            }
            bool mouseCaptureReady =
                mouseHook != IntPtr.Zero &&
                !session.Suspended &&
                session.RemoteWidth > 0 &&
                session.RemoteHeight > 0 &&
                session.RemoteBounds.Width >= 1 &&
                session.RemoteBounds.Height >= 1;
            keyboardCaptureEnabled =
                mouseCaptureReady &&
                session.KeyboardEnabled &&
                keyboardHook != IntPtr.Zero &&
                !session.Suspended;
            return mouseCaptureReady && (!session.KeyboardEnabled || keyboardHook != IntPtr.Zero);
        }

        private Rectangle BrowserRectangleToClient(double left, double top, double width, double height)
        {
            RectangleF screen = BrowserRectangleToScreen(left, top, width, height);
            if (screen.IsEmpty)
            {
                return Rectangle.Empty;
            }
            Point topLeft = PointToClient(new Point(
                (int)Math.Round(screen.Left),
                (int)Math.Round(screen.Top)));
            Point bottomRight = PointToClient(new Point(
                (int)Math.Round(screen.Right),
                (int)Math.Round(screen.Bottom)));
            return Rectangle.FromLTRB(topLeft.X, topLeft.Y, bottomRight.X, bottomRight.Y);
        }

        private bool ConfigureNativeVideo(object payload)
        {
            Dictionary<string, object> values = payload as Dictionary<string, object>;
            if (values == null || !ReadBoolean(values, "enabled", false))
            {
                if (nativeVideoHandle != IntPtr.Zero)
                {
                    LANRemoteVideoStop(nativeVideoHandle);
                }
                return true;
            }
            if (!remoteWindow || nativeVideoHandle == IntPtr.Zero)
            {
                return false;
            }
            Uri endpoint;
            string endpointValue = ReadString(values, "endpoint");
            string token = ReadString(values, "token");
            string monitor = ReadString(values, "monitor");
            int fps = ReadInteger(values, "fps_limit", 60);
            Rectangle surface = BrowserRectangleToClient(
                ReadDouble(values, "surface_left", 0),
                ReadDouble(values, "surface_top", 0),
                ReadDouble(values, "surface_width", 0),
                ReadDouble(values, "surface_height", 0));
            if (
                !Uri.TryCreate(endpointValue, UriKind.Absolute, out endpoint) ||
                !String.Equals(endpoint.Scheme, Uri.UriSchemeHttp, StringComparison.OrdinalIgnoreCase) ||
                !String.Equals(endpoint.AbsolutePath, "/video-stream", StringComparison.Ordinal) ||
                !String.IsNullOrEmpty(endpoint.UserInfo) ||
                endpoint.Port < 1 || endpoint.Port > 65535 ||
                String.IsNullOrWhiteSpace(token) || token.Length > 512 ||
                token.IndexOfAny(new[] { '\r', '\n' }) >= 0 ||
                (fps != 30 && fps != 60 && fps != 120) ||
                surface.Width < 1 || surface.Height < 1)
            {
                return false;
            }
            int configured = LANRemoteVideoConfigure(
                nativeVideoHandle,
                endpoint.Host,
                (uint)endpoint.Port,
                token,
                String.IsNullOrWhiteSpace(monitor) ? "all" : monitor,
                (uint)fps,
                surface.Left,
                surface.Top,
                surface.Width,
                surface.Height);
            if (configured != 0)
            {
                LANRemoteVideoSetScaleMode(
                    nativeVideoHandle,
                    String.Equals(ReadString(values, "scale_mode"), "fill", StringComparison.Ordinal) ? 1 : 0);
            }
            return configured != 0;
        }

        private bool SetNativeVideoLayout(object payload)
        {
            Dictionary<string, object> values = payload as Dictionary<string, object>;
            if (values == null || nativeVideoHandle == IntPtr.Zero)
            {
                return false;
            }
            Rectangle surface = BrowserRectangleToClient(
                ReadDouble(values, "surface_left", 0),
                ReadDouble(values, "surface_top", 0),
                ReadDouble(values, "surface_width", 0),
                ReadDouble(values, "surface_height", 0));
            if (surface.Width < 1 || surface.Height < 1)
            {
                return false;
            }
            LANRemoteVideoSetLayout(
                nativeVideoHandle,
                surface.Left,
                surface.Top,
                surface.Width,
                surface.Height,
                ReadBoolean(values, "visible", true) ? 1 : 0);
            LANRemoteVideoSetExclusions(nativeVideoHandle, new int[0], 0);
            LANRemoteVideoSetScaleMode(
                nativeVideoHandle,
                String.Equals(ReadString(values, "scale_mode"), "fill", StringComparison.Ordinal) ? 1 : 0);
            return true;
        }

        private bool SetNativeOverlayState(object payload)
        {
            Dictionary<string, object> values = payload as Dictionary<string, object>;
            if (!remoteWindow || values == null || nativeToolbar == null)
            {
                return false;
            }
            nativeOverlayRequestedVisible = ReadBoolean(values, "visible", false);
            nativeToolbar.UpdateState(values);
            UpdateNativeOverlayVisibility();
            return true;
        }

        private void UpdateNativeOverlayVisibility()
        {
            if (!remoteWindow || nativeToolbar == null) return;
            bool ownerVisible = Visible && WindowState != FormWindowState.Minimized;
            if (nativeOverlayRequestedVisible && ownerVisible)
            {
                if (!nativeToolbar.Visible) nativeToolbar.Show(this);
                PositionNativeOverlays();
                nativeToolbar.BringToFront();
            }
            else
            {
                if (nativeToolbar.Visible) nativeToolbar.Hide();
            }
        }

        private void PositionNativeOverlays()
        {
            if (!IsHandleCreated || nativeToolbar == null) return;
            Point origin = PointToScreen(Point.Empty);
            Rectangle ownerBounds = new Rectangle(origin, ClientSize);
            nativeToolbar.PositionForOwner(
                ownerBounds,
                new Point(
                    origin.X + Math.Max(0, (ClientSize.Width - nativeToolbar.Width) / 2),
                    origin.Y + 48));
        }

        private void SendNativeOverlayAction(string action, string value)
        {
            if (String.Equals(action, "drag", StringComparison.Ordinal))
            {
                ReleaseCapture();
                SendMessage(Handle, WmNcLButtonDown, new IntPtr(HtCaption), IntPtr.Zero);
                return;
            }
            if (browser.CoreWebView2 == null) return;
            string script = "window.__lanNativeOverlayAction&&window.__lanNativeOverlayAction(" +
                serializer.Serialize(action ?? String.Empty) + "," +
                serializer.Serialize(value ?? String.Empty) + ");";
            browser.CoreWebView2.ExecuteScriptAsync(script);
        }

        private object NativeVideoStatus()
        {
            if (nativeVideoHandle == IntPtr.Zero)
            {
                return new Dictionary<string, object>
                {
                    { "state", "unavailable" },
                    { "transport", "mjpeg_v1" },
                    { "error", nativeVideoUnavailableReason }
                };
            }
            StringBuilder output = new StringBuilder(4096);
            int length = LANRemoteVideoGetStatus(nativeVideoHandle, output, output.Capacity);
            if (length <= 0)
            {
                return new Dictionary<string, object>
                {
                    { "state", "failed" },
                    { "transport", "mjpeg_v1" },
                    { "error", "native video status unavailable" }
                };
            }
            try
            {
                return serializer.DeserializeObject(output.ToString());
            }
            catch
            {
                return new Dictionary<string, object>
                {
                    { "state", "failed" },
                    { "transport", "mjpeg_v1" },
                    { "error", "native video status is malformed" }
                };
            }
        }

        private bool SetNativeVideoCursor(object payload)
        {
            Dictionary<string, object> values = payload as Dictionary<string, object>;
            if (values == null || nativeVideoHandle == IntPtr.Zero)
            {
                return false;
            }
            LANRemoteVideoSetCursor(
                nativeVideoHandle,
                ReadInteger(values, "x", 0),
                ReadInteger(values, "y", 0),
                ReadBoolean(values, "visible", false) ? 1 : 0,
                ReadInteger(values, "remote_width", 0),
                ReadInteger(values, "remote_height", 0),
                ReadBoolean(values, "remote_owner", false) ? 1 : 0);
            return true;
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
            string dataFolder = BrowserDataFolder();
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

        private string BrowserDataFolder()
        {
            return Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "LAN Remote",
                remoteWindow ? "ControlHostWebView2-Remote" : "ControlHostWebView2");
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
                case "configure_native_video":
                    result = ConfigureNativeVideo(payload);
                    break;
                case "set_native_video_layout":
                    result = SetNativeVideoLayout(payload);
                    break;
                case "set_native_overlay_state":
                    result = SetNativeOverlayState(payload);
                    break;
                case "native_video_status":
                    result = NativeVideoStatus();
                    break;
                case "set_native_video_cursor":
                    result = SetNativeVideoCursor(payload);
                    break;
                case "release_native_input":
                    ReleaseNativePressedInputs();
                    result = true;
                    break;
                case "activate_remote_window":
                    int processId;
                    try
                    {
                        processId = Convert.ToInt32(payload);
                    }
                    catch (FormatException)
                    {
                        processId = 0;
                    }
                    catch (InvalidCastException)
                    {
                        processId = 0;
                    }
                    catch (OverflowException)
                    {
                        processId = 0;
                    }
                    result = await ActivateRemoteWindow(processId);
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
            UpdateNativeOverlayVisibility();
            ApplyWindowCornerPreference();
        }

        protected override void OnSizeChanged(EventArgs e)
        {
            base.OnSizeChanged(e);
            if (!fullscreen && WindowState != FormWindowState.Minimized)
            {
                maximized = WindowState == FormWindowState.Maximized;
            }
            ApplyWindowCornerPreference();
            UpdateNativeOverlayVisibility();
        }

        protected override void OnLocationChanged(EventArgs e)
        {
            base.OnLocationChanged(e);
            PositionNativeOverlays();
        }

        protected override void OnVisibleChanged(EventArgs e)
        {
            base.OnVisibleChanged(e);
            UpdateNativeOverlayVisibility();
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
            if (nativeToolbar != null)
            {
                nativeToolbar.Dispose();
                nativeToolbar = null;
            }
            if (nativeVideoHandle != IntPtr.Zero)
            {
                LANRemoteVideoDestroy(nativeVideoHandle);
                nativeVideoHandle = IntPtr.Zero;
            }
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
    configure_native_video: (config) => call('configure_native_video', config || {enabled: false}),
    set_native_video_layout: (layout) => call('set_native_video_layout', layout || {}),
    set_native_overlay_state: (state) => call('set_native_overlay_state', state || {visible: false}),
    native_video_status: () => call('native_video_status'),
    set_native_video_cursor: (cursor) => call('set_native_video_cursor', cursor || {}),
    activate_remote_window: (processId) => call('activate_remote_window', Number(processId) || 0),
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
    try_auto_unlock: (deviceJson, token, force) => fetch('/api/native/try-auto-unlock', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({device: JSON.parse(deviceJson), token, force: Boolean(force)}),
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
