using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Security.Principal;
using System.Text;
using System.Threading;
using System.Windows.Forms;

namespace WindowsLANRemoteSetup
{
    internal static class Program
    {
        [STAThread]
        private static int Main(string[] args)
        {
            // Build verification uses this to prove the setup executable can
            // be started with CreateProcess without triggering UAC.
            if (HasArgument(args, "--launch-probe"))
            {
                return 0;
            }

            bool quiet = IsQuiet(args);
            if (!IsAdministrator())
            {
                return RelaunchAsAdministrator(args, quiet);
            }

            // Give an in-app updater enough time to return its HTTP response
            // before install.ps1 stops the currently running old version.
            Thread.Sleep(1200);

            string tempDir = Path.Combine(Path.GetTempPath(), "WindowsLANRemoteSetup-" + Guid.NewGuid().ToString("N"));

            try
            {
                Directory.CreateDirectory(tempDir);
                ExtractPayload(tempDir);

                string installScript = Path.Combine(tempDir, "install.ps1");
                if (!File.Exists(installScript))
                {
                    throw new FileNotFoundException("Installer payload is missing install.ps1.");
                }

                ProcessStartInfo startInfo = new ProcessStartInfo();
                startInfo.FileName = "powershell.exe";
                startInfo.Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File \"" + installScript + "\"";
                startInfo.WorkingDirectory = tempDir;
                startInfo.UseShellExecute = false;
                startInfo.CreateNoWindow = true;
                startInfo.RedirectStandardOutput = true;
                startInfo.RedirectStandardError = true;
                startInfo.WindowStyle = ProcessWindowStyle.Hidden;

                using (Process process = Process.Start(startInfo))
                {
                    if (process == null)
                    {
                        throw new InvalidOperationException("Could not start installer payload.");
                    }

                    string standardOutput = process.StandardOutput.ReadToEnd();
                    string standardError = process.StandardError.ReadToEnd();
                    process.WaitForExit();
                    if (process.ExitCode != 0)
                    {
                        string details = !String.IsNullOrWhiteSpace(standardError) ? standardError : standardOutput;
                        if (String.IsNullOrWhiteSpace(details))
                        {
                            details = "No additional error details were returned.";
                        }

                        throw new InvalidOperationException(
                            "Installer payload exited with code " + process.ExitCode + ".\n\n" + details.Trim());
                    }
                }

                if (!quiet)
                {
                    MessageBox.Show(
                        "Windows LAN Remote has been installed. Open it from the Start menu.",
                        "Windows LAN Remote Setup",
                        MessageBoxButtons.OK,
                        MessageBoxIcon.Information);
                }

                return 0;
            }
            catch (Exception ex)
            {
                if (!quiet)
                {
                    MessageBox.Show(
                        "Windows LAN Remote could not be installed.\n\n" + ex.Message,
                        "Windows LAN Remote Setup",
                        MessageBoxButtons.OK,
                        MessageBoxIcon.Error);
                }

                return 1;
            }
            finally
            {
                TryDelete(tempDir);
            }
        }

        private static bool IsAdministrator()
        {
            WindowsIdentity identity = WindowsIdentity.GetCurrent();
            WindowsPrincipal principal = new WindowsPrincipal(identity);
            return principal.IsInRole(WindowsBuiltInRole.Administrator);
        }

        private static int RelaunchAsAdministrator(string[] args, bool quiet)
        {
            try
            {
                ProcessStartInfo startInfo = new ProcessStartInfo();
                startInfo.FileName = Application.ExecutablePath;
                startInfo.Arguments = BuildElevatedArguments(args);
                startInfo.UseShellExecute = true;
                startInfo.Verb = "runas";
                startInfo.WorkingDirectory = Path.GetDirectoryName(Application.ExecutablePath);

                using (Process process = Process.Start(startInfo))
                {
                    if (process == null)
                    {
                        throw new InvalidOperationException("Could not request administrator permission.");
                    }
                    process.WaitForExit();
                    return process.ExitCode;
                }
            }
            catch (Exception ex)
            {
                if (!quiet)
                {
                    MessageBox.Show(
                        "Windows LAN Remote needs administrator permission to install.\n\n" + ex.Message,
                        "Windows LAN Remote Setup",
                        MessageBoxButtons.OK,
                        MessageBoxIcon.Warning);
                }
                return 1;
            }
        }

        private static string BuildElevatedArguments(string[] args)
        {
            StringBuilder result = new StringBuilder();
            if (args != null)
            {
                foreach (string arg in args)
                {
                    if (String.Equals(arg, "--elevated", StringComparison.OrdinalIgnoreCase))
                    {
                        continue;
                    }
                    if (result.Length > 0) result.Append(' ');
                    result.Append('"').Append(arg.Replace("\"", "\\\"")).Append('"');
                }
            }
            if (result.Length > 0) result.Append(' ');
            result.Append("--elevated");
            return result.ToString();
        }

        private static bool IsQuiet(string[] args)
        {
            if (args == null)
            {
                return false;
            }

            foreach (string arg in args)
            {
                if (string.Equals(arg, "/quiet", StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(arg, "-quiet", StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(arg, "--quiet", StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }
            }

            return false;
        }

        private static bool HasArgument(string[] args, string value)
        {
            if (args == null)
            {
                return false;
            }
            foreach (string arg in args)
            {
                if (String.Equals(arg, value, StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }
            }
            return false;
        }

        private static void ExtractPayload(string destination)
        {
            Assembly assembly = Assembly.GetExecutingAssembly();
            using (Stream stream = assembly.GetManifestResourceStream("Payload.zip"))
            {
                if (stream == null)
                {
                    throw new InvalidOperationException("Installer payload resource was not found.");
                }

                using (ZipArchive archive = new ZipArchive(stream, ZipArchiveMode.Read))
                {
                    archive.ExtractToDirectory(destination);
                }
            }
        }

        private static void TryDelete(string path)
        {
            try
            {
                if (Directory.Exists(path))
                {
                    Directory.Delete(path, true);
                }
            }
            catch
            {
                // Temporary extraction files are safe to leave behind if Windows is still releasing handles.
            }
        }
    }
}
