param(
    [Parameter(Mandatory = $true)]
    [string]$InstallScript
)

$ErrorActionPreference = "Stop"
. $InstallScript -FunctionsOnly

$InstallRoot = "C:\Program Files\Windows LAN Remote"
$Processes = @(
    [pscustomobject]@{ ProcessId = 100; ParentProcessId = 1; Name = "WindowsLANRemote-0.6.7.exe"; ExecutablePath = "$InstallRoot\WindowsLANRemote-0.6.7.exe" },
    [pscustomobject]@{ ProcessId = 200; ParentProcessId = 100; Name = "WindowsLANRemoteSetup-0.6.8.exe"; ExecutablePath = "C:\Temp\WindowsLANRemoteSetup-0.6.8.exe" },
    [pscustomobject]@{ ProcessId = 201; ParentProcessId = 4; Name = "WindowsLANRemoteSetup-0.6.8.exe"; ExecutablePath = "C:\Temp\WindowsLANRemoteSetup-0.6.8.exe" },
    [pscustomobject]@{ ProcessId = 202; ParentProcessId = 201; Name = "powershell.exe"; ExecutablePath = "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" },
    [pscustomobject]@{ ProcessId = 300; ParentProcessId = 100; Name = "WindowsLANRemoteControlHost.exe"; ExecutablePath = "$InstallRoot\WindowsLANRemoteControlHost.exe" },
    [pscustomobject]@{ ProcessId = 400; ParentProcessId = 300; Name = "msedgewebview2.exe"; ExecutablePath = "C:\Program Files (x86)\Microsoft\EdgeWebView\Application\msedgewebview2.exe" },
    [pscustomobject]@{ ProcessId = 500; ParentProcessId = 1; Name = "unrelated.exe"; ExecutablePath = "C:\Tools\unrelated.exe" }
)

$Selection = Get-InstalledProcessSelection `
    -Processes $Processes `
    -Roots @($InstallRoot, "C:\Users\test\AppData\Local\Programs\Windows LAN Remote") `
    -CurrentProcessId 202

$ActualStopped = @($Selection.ProcessIds | Sort-Object)
$ExpectedStopped = @(100, 300, 400)
if (($ActualStopped -join ',') -ne ($ExpectedStopped -join ',')) {
    throw "Unexpected stop selection: $($ActualStopped -join ','); expected: $($ExpectedStopped -join ',')."
}

foreach ($ProtectedId in @(200, 201, 202)) {
    if ($ActualStopped -contains $ProtectedId) {
        throw "Updater process $ProtectedId must never be selected for termination."
    }
}
if ($ActualStopped -contains 500) {
    throw "Unrelated process was selected for termination."
}

Write-Output "INSTALL_PROCESS_SELECTION_TESTS_OK"
