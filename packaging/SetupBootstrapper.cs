using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Windows.Forms;

namespace WindowsLANRemoteSetup
{
    internal static class Program
    {
        [STAThread]
        private static int Main(string[] args)
        {
            string tempDir = Path.Combine(Path.GetTempPath(), "WindowsLANRemoteSetup-" + Guid.NewGuid().ToString("N"));
            bool quiet = IsQuiet(args);

            try
            {
                Directory.CreateDirectory(tempDir);
                ExtractPayload(tempDir);

                string installCmd = Path.Combine(tempDir, "install.cmd");
                if (!File.Exists(installCmd))
                {
                    throw new FileNotFoundException("Installer payload is missing install.cmd.");
                }

                ProcessStartInfo startInfo = new ProcessStartInfo();
                startInfo.FileName = installCmd;
                startInfo.WorkingDirectory = tempDir;
                startInfo.UseShellExecute = true;
                startInfo.WindowStyle = ProcessWindowStyle.Normal;

                using (Process process = Process.Start(startInfo))
                {
                    if (process == null)
                    {
                        throw new InvalidOperationException("Could not start installer payload.");
                    }

                    process.WaitForExit();
                    if (process.ExitCode != 0)
                    {
                        throw new InvalidOperationException("Installer payload exited with code " + process.ExitCode + ".");
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
