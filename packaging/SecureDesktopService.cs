using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.ServiceProcess;
using System.Text;
using System.Threading;

namespace WindowsLANRemoteSecureDesktop
{
    internal static class Program
    {
        private static void Main()
        {
            ServiceBase.Run(new SecureDesktopService());
        }
    }

    internal sealed class SecureDesktopService : ServiceBase
    {
        private const int SecureHelperPort = 8767;
        private readonly ManualResetEvent stopEvent = new ManualResetEvent(false);
        private Thread worker;
        private Process helper;
        private int helperSession = -1;
        private IntPtr helperJob = IntPtr.Zero;
        private string lastError = String.Empty;

        public SecureDesktopService()
        {
            ServiceName = "WindowsLANRemoteSecureDesktop";
            CanStop = true;
            CanShutdown = true;
            CanHandleSessionChangeEvent = true;
            AutoLog = false;
        }

        protected override void OnStart(string[] args)
        {
            WriteLog("Service starting.");
            stopEvent.Reset();
            worker = new Thread(WorkerLoop);
            worker.IsBackground = true;
            worker.Name = "LAN Remote secure desktop monitor";
            worker.Start();
        }

        protected override void OnStop()
        {
            stopEvent.Set();
            if (worker != null && worker.IsAlive)
            {
                worker.Join(8000);
            }
            StopHelper();
            WriteLog("Service stopped.");
        }

        protected override void OnShutdown()
        {
            OnStop();
            base.OnShutdown();
        }

        protected override void OnSessionChange(SessionChangeDescription changeDescription)
        {
            StopHelper();
            base.OnSessionChange(changeDescription);
        }

        private void WorkerLoop()
        {
            while (!stopEvent.WaitOne(0))
            {
                try
                {
                    EnsureHelper();
                }
                catch (Exception ex)
                {
                    string message = ex.GetType().Name + ": " + ex.Message;
                    if (!String.Equals(message, lastError, StringComparison.Ordinal))
                    {
                        WriteLog("Helper launch failed: " + message);
                        lastError = message;
                    }
                    StopHelper();
                }
                stopEvent.WaitOne(2000);
            }
        }

        private void EnsureHelper()
        {
            int sessionId = unchecked((int)WTSGetActiveConsoleSessionId());
            if (sessionId == -1)
            {
                StopHelper();
                return;
            }

            if (helper != null && !helper.HasExited && helperSession == sessionId)
            {
                return;
            }

            StopHelper();
            helper = LaunchHelper(sessionId);
            helperSession = sessionId;
            lastError = String.Empty;
            WriteLog("Secure helper started in session " + sessionId + ".");
        }

        private Process LaunchHelper(int sessionId)
        {
            string servicePath = Assembly.GetExecutingAssembly().Location;
            string serviceName = Path.GetFileName(servicePath);
            string appName = serviceName.Replace("WindowsLANRemoteService-", "WindowsLANRemote-");
            string appPath = Path.Combine(Path.GetDirectoryName(servicePath), appName);
            string tokenPath = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
                "Windows LAN Remote",
                "service-token.txt");

            if (!File.Exists(appPath) || !File.Exists(tokenPath))
            {
                throw new FileNotFoundException("LAN Remote secure desktop payload is incomplete.");
            }

            Process winlogon = null;
            foreach (Process process in Process.GetProcessesByName("winlogon"))
            {
                try
                {
                    if (process.SessionId == sessionId)
                    {
                        winlogon = process;
                        break;
                    }
                }
                catch
                {
                    process.Dispose();
                }
            }
            if (winlogon == null)
            {
                throw new InvalidOperationException("Winlogon was not found for the active console session.");
            }

            IntPtr sourceToken = IntPtr.Zero;
            IntPtr primaryToken = IntPtr.Zero;
            IntPtr job = IntPtr.Zero;
            PROCESS_INFORMATION processInfo = new PROCESS_INFORMATION();
            try
            {
                const uint tokenRights = TOKEN_ASSIGN_PRIMARY | TOKEN_DUPLICATE | TOKEN_QUERY;
                if (!OpenProcessToken(winlogon.Handle, tokenRights, out sourceToken))
                {
                    throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
                }
                if (!DuplicateTokenEx(
                    sourceToken,
                    MAXIMUM_ALLOWED,
                    IntPtr.Zero,
                    SECURITY_IMPERSONATION_LEVEL.SecurityImpersonation,
                    TOKEN_TYPE.TokenPrimary,
                    out primaryToken))
                {
                    throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
                }

                STARTUPINFO startupInfo = new STARTUPINFO();
                startupInfo.cb = Marshal.SizeOf(typeof(STARTUPINFO));
                startupInfo.lpDesktop = @"winsta0\Winlogon";
                startupInfo.dwFlags = STARTF_USESHOWWINDOW;
                startupInfo.wShowWindow = 0;

                StringBuilder commandLine = new StringBuilder();
                commandLine.Append('"').Append(appPath).Append('"');
                commandLine.Append(" --secure-helper --secure-port ").Append(SecureHelperPort);
                commandLine.Append(" --secure-secret-file \"").Append(tokenPath).Append("\"");

                job = CreateKillOnCloseJob();
                uint creationFlags = CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT | CREATE_SUSPENDED;
                if (!CreateProcessAsUser(
                    primaryToken,
                    appPath,
                    commandLine,
                    IntPtr.Zero,
                    IntPtr.Zero,
                    false,
                    creationFlags,
                    IntPtr.Zero,
                    Path.GetDirectoryName(appPath),
                    ref startupInfo,
                    out processInfo))
                {
                    throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
                }

                if (!AssignProcessToJobObject(job, processInfo.hProcess))
                {
                    throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
                }
                if (ResumeThread(processInfo.hThread) == 0xFFFFFFFF)
                {
                    throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
                }

                Process launched = Process.GetProcessById(unchecked((int)processInfo.dwProcessId));
                helperJob = job;
                job = IntPtr.Zero;
                return launched;
            }
            finally
            {
                if (job != IntPtr.Zero) CloseHandle(job);
                if (processInfo.hThread != IntPtr.Zero) CloseHandle(processInfo.hThread);
                if (processInfo.hProcess != IntPtr.Zero) CloseHandle(processInfo.hProcess);
                if (primaryToken != IntPtr.Zero) CloseHandle(primaryToken);
                if (sourceToken != IntPtr.Zero) CloseHandle(sourceToken);
                winlogon.Dispose();
            }
        }

        private void StopHelper()
        {
            Process current = helper;
            helper = null;
            helperSession = -1;
            IntPtr job = helperJob;
            helperJob = IntPtr.Zero;
            if (job != IntPtr.Zero) CloseHandle(job);
            if (current == null) return;
            try
            {
                if (!current.HasExited && job == IntPtr.Zero) current.Kill();
                current.WaitForExit(3000);
            }
            catch
            {
            }
            finally
            {
                current.Dispose();
            }
        }

        private static void WriteLog(string message)
        {
            try
            {
                string directory = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
                    "Windows LAN Remote");
                Directory.CreateDirectory(directory);
                File.AppendAllText(
                    Path.Combine(directory, "service.log"),
                    "[" + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "] " + message + Environment.NewLine,
                    Encoding.UTF8);
            }
            catch
            {
            }
        }

        private const uint TOKEN_ASSIGN_PRIMARY = 0x0001;
        private const uint TOKEN_DUPLICATE = 0x0002;
        private const uint TOKEN_QUERY = 0x0008;
        private const uint MAXIMUM_ALLOWED = 0x02000000;
        private const uint CREATE_UNICODE_ENVIRONMENT = 0x00000400;
        private const uint CREATE_NO_WINDOW = 0x08000000;
        private const uint CREATE_SUSPENDED = 0x00000004;
        private const int STARTF_USESHOWWINDOW = 0x00000001;
        private const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;

        private static IntPtr CreateKillOnCloseJob()
        {
            IntPtr job = CreateJobObject(IntPtr.Zero, null);
            if (job == IntPtr.Zero)
            {
                throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            }
            JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            int length = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
            IntPtr data = Marshal.AllocHGlobal(length);
            try
            {
                Marshal.StructureToPtr(limits, data, false);
                if (!SetInformationJobObject(job, 9, data, (uint)length))
                {
                    throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
                }
            }
            catch
            {
                CloseHandle(job);
                throw;
            }
            finally
            {
                Marshal.FreeHGlobal(data);
            }
            return job;
        }

        private enum SECURITY_IMPERSONATION_LEVEL
        {
            SecurityAnonymous,
            SecurityIdentification,
            SecurityImpersonation,
            SecurityDelegation
        }

        private enum TOKEN_TYPE
        {
            TokenPrimary = 1,
            TokenImpersonation
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct STARTUPINFO
        {
            public int cb;
            public string lpReserved;
            public string lpDesktop;
            public string lpTitle;
            public int dwX;
            public int dwY;
            public int dwXSize;
            public int dwYSize;
            public int dwXCountChars;
            public int dwYCountChars;
            public int dwFillAttribute;
            public int dwFlags;
            public short wShowWindow;
            public short cbReserved2;
            public IntPtr lpReserved2;
            public IntPtr hStdInput;
            public IntPtr hStdOutput;
            public IntPtr hStdError;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct PROCESS_INFORMATION
        {
            public IntPtr hProcess;
            public IntPtr hThread;
            public uint dwProcessId;
            public uint dwThreadId;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
        {
            public long PerProcessUserTimeLimit;
            public long PerJobUserTimeLimit;
            public uint LimitFlags;
            public UIntPtr MinimumWorkingSetSize;
            public UIntPtr MaximumWorkingSetSize;
            public uint ActiveProcessLimit;
            public UIntPtr Affinity;
            public uint PriorityClass;
            public uint SchedulingClass;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct IO_COUNTERS
        {
            public ulong ReadOperationCount;
            public ulong WriteOperationCount;
            public ulong OtherOperationCount;
            public ulong ReadTransferCount;
            public ulong WriteTransferCount;
            public ulong OtherTransferCount;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        {
            public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
            public IO_COUNTERS IoInfo;
            public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit;
            public UIntPtr PeakProcessMemoryUsed;
            public UIntPtr PeakJobMemoryUsed;
        }

        [DllImport("kernel32.dll")]
        private static extern uint WTSGetActiveConsoleSessionId();

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool OpenProcessToken(IntPtr processHandle, uint desiredAccess, out IntPtr tokenHandle);

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool DuplicateTokenEx(
            IntPtr existingToken,
            uint desiredAccess,
            IntPtr tokenAttributes,
            SECURITY_IMPERSONATION_LEVEL impersonationLevel,
            TOKEN_TYPE tokenType,
            out IntPtr newToken);

        [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        private static extern bool CreateProcessAsUser(
            IntPtr token,
            string applicationName,
            StringBuilder commandLine,
            IntPtr processAttributes,
            IntPtr threadAttributes,
            bool inheritHandles,
            uint creationFlags,
            IntPtr environment,
            string currentDirectory,
            ref STARTUPINFO startupInfo,
            out PROCESS_INFORMATION processInformation);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr handle);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateJobObject(IntPtr jobAttributes, string name);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetInformationJobObject(IntPtr job, int informationClass, IntPtr information, uint informationLength);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint ResumeThread(IntPtr thread);
    }
}
