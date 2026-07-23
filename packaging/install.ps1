param(
    [switch]$FunctionsOnly
)

$ErrorActionPreference = "Stop"

$AppName = "Windows LAN Remote"
$AppId = "WindowsLANRemote"
$Publisher = "EmpK1019"
$ServiceName = "WindowsLANRemoteSecureDesktop"
$VersionFile = Join-Path $PSScriptRoot "VERSION.txt"
$Version = if (Test-Path -LiteralPath $VersionFile) { (Get-Content -Raw -LiteralPath $VersionFile).Trim() } else { "1.1.1" }

$InstallDir = Join-Path $env:ProgramFiles $AppName
$LegacyInstallDir = Join-Path $env:LOCALAPPDATA "Programs\$AppName"
$StartMenuDir = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\$AppName"
$LegacyStartMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$AppName"
$UninstallKey = "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppId"
$LegacyUninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppId"
$ExecutableName = "WindowsLANRemote-$Version.exe"
$ServiceExecutableName = "WindowsLANRemoteService-$Version.exe"
$SourceAppDir = Join-Path $PSScriptRoot "app"
$SourceExecutable = Join-Path $SourceAppDir $ExecutableName
$SourceServiceExecutable = Join-Path $PSScriptRoot "WindowsLANRemoteService.exe"
$InstalledExecutable = Join-Path $InstallDir $ExecutableName
$InstalledServiceExecutable = Join-Path $InstallDir $ServiceExecutableName
$ServiceDataDir = Join-Path $env:ProgramData $AppName
$ServiceTokenPath = Join-Path $ServiceDataDir "service-token.txt"
$LogPath = Join-Path $env:TEMP "WindowsLANRemote-install.log"

function Write-InstallLog {
    param([string]$Message)
    $Timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $LogPath -Value "[$Timestamp] $Message" -Encoding UTF8
}

function Assert-Administrator {
    $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $Principal = New-Object Security.Principal.WindowsPrincipal($Identity)
    if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Administrator permission is required to install the secure desktop service."
    }
}

function Copy-WithRetry {
    param([string]$Source, [string]$Destination, [int]$Attempts = 8)
    for ($Attempt = 1; $Attempt -le $Attempts; $Attempt++) {
        try {
            Copy-Item -LiteralPath $Source -Destination $Destination -Force
            return
        }
        catch {
            if ($Attempt -eq $Attempts) { throw }
            Start-Sleep -Milliseconds (300 * $Attempt)
        }
    }
}

function Copy-ApplicationWithRetry {
    param([string]$Source, [string]$Destination, [int]$Attempts = 8)
    if (-not (Test-Path -LiteralPath $SourceExecutable)) {
        throw "Installer payload is missing $ExecutableName."
    }
    for ($Attempt = 1; $Attempt -le $Attempts; $Attempt++) {
        try {
            Get-ChildItem -LiteralPath $Source -Force | Copy-Item -Destination $Destination -Recurse -Force
            return
        }
        catch {
            if ($Attempt -eq $Attempts) { throw }
            Start-Sleep -Milliseconds (300 * $Attempt)
        }
    }
}

function Reset-InstallDirectory {
    $Expected = [IO.Path]::GetFullPath((Join-Path $env:ProgramFiles $AppName)).TrimEnd('\')
    $Actual = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
    if (-not $Actual.Equals($Expected, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to replace unexpected installation directory: $Actual"
    }
    for ($Attempt = 1; $Attempt -le 12; $Attempt++) {
        try {
            if (Test-Path -LiteralPath $InstallDir) {
                Get-ChildItem -LiteralPath $InstallDir -Force |
                    Remove-Item -Recurse -Force
            }
            else {
                New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
            }
            break
        }
        catch {
            if ($Attempt -eq 12) { throw }
            Start-Sleep -Milliseconds (350 * $Attempt)
        }
    }
}

function Test-ProcessPathInRoots {
    param([string]$ProcessPath, [string[]]$Roots)
    if ([string]::IsNullOrWhiteSpace($ProcessPath)) { return $false }
    try {
        $Candidate = [IO.Path]::GetFullPath($ProcessPath)
        foreach ($Root in $Roots) {
            $Prefix = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
            if ($Candidate.StartsWith($Prefix, [StringComparison]::OrdinalIgnoreCase)) {
                return $true
            }
        }
    }
    catch { }
    return $false
}

function Get-InstalledProcessSelection {
    param(
        [object[]]$Processes,
        [string[]]$Roots,
        [int]$CurrentProcessId
    )

    $ProtectedProcessIds = New-Object 'System.Collections.Generic.HashSet[int]'
    $ProtectedProcessIds.Add($CurrentProcessId) | Out-Null
    foreach ($Process in $Processes) {
        $ProcessPath = [string]$Process.ExecutablePath
        $ProcessName = [string]$Process.Name
        if (
            -not (Test-ProcessPathInRoots -ProcessPath $ProcessPath -Roots $Roots) -and
            $ProcessName -like 'WindowsLANRemoteSetup*.exe'
        ) {
            $ProtectedProcessIds.Add([int]$Process.ProcessId) | Out-Null
        }
    }

    $RootProcessIds = New-Object 'System.Collections.Generic.HashSet[int]'
    foreach ($Process in $Processes) {
        if (Test-ProcessPathInRoots -ProcessPath ([string]$Process.ExecutablePath) -Roots $Roots) {
            $RootProcessIds.Add([int]$Process.ProcessId) | Out-Null
        }
    }

    $AllProcessIds = New-Object 'System.Collections.Generic.HashSet[int]'
    foreach ($ProcessId in $RootProcessIds) { $AllProcessIds.Add($ProcessId) | Out-Null }
    do {
        $Added = $false
        foreach ($Process in $Processes) {
            $ProcessId = [int]$Process.ProcessId
            if (
                -not $ProtectedProcessIds.Contains($ProcessId) -and
                $AllProcessIds.Contains([int]$Process.ParentProcessId) -and
                $AllProcessIds.Add($ProcessId)
            ) {
                $Added = $true
            }
        }
    } while ($Added)

    return [pscustomobject]@{
        ProcessIds = @($AllProcessIds | Sort-Object)
        ProtectedProcessIds = @($ProtectedProcessIds | Sort-Object)
    }
}

function Stop-InstalledProcesses {
    $Roots = @($InstallDir, $LegacyInstallDir)
    $Processes = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $Selection = Get-InstalledProcessSelection -Processes $Processes -Roots $Roots -CurrentProcessId $PID
    Write-InstallLog "Stopping installed process IDs: $($Selection.ProcessIds -join ','); protected updater IDs: $($Selection.ProtectedProcessIds -join ',')."
    foreach ($ProcessId in $Selection.ProcessIds) {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }
    foreach ($ProcessId in $Selection.ProcessIds) {
        Wait-Process -Id $ProcessId -Timeout 8 -ErrorAction SilentlyContinue
    }
}

function Ensure-ServiceToken {
    New-Item -ItemType Directory -Force -Path $ServiceDataDir | Out-Null
    if (-not (Test-Path -LiteralPath $ServiceTokenPath)) {
        $Bytes = New-Object byte[] 32
        $Generator = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $Generator.GetBytes($Bytes) } finally { $Generator.Dispose() }
        $Token = -join ($Bytes | ForEach-Object { $_.ToString("x2") })
        Set-Content -LiteralPath $ServiceTokenPath -Value $Token -NoNewline -Encoding ASCII
    }

    $Acl = New-Object Security.AccessControl.FileSecurity
    $Acl.SetAccessRuleProtection($true, $false)
    $Rules = @(
        @("S-1-5-18", [Security.AccessControl.FileSystemRights]::FullControl),
        @("S-1-5-32-544", [Security.AccessControl.FileSystemRights]::FullControl),
        @("S-1-5-32-545", [Security.AccessControl.FileSystemRights]::Read)
    )
    foreach ($Entry in $Rules) {
        $Sid = New-Object Security.Principal.SecurityIdentifier($Entry[0])
        $Rule = New-Object Security.AccessControl.FileSystemAccessRule(
            $Sid,
            $Entry[1],
            [Security.AccessControl.AccessControlType]::Allow)
        $Acl.AddAccessRule($Rule)
    }
    Set-Acl -LiteralPath $ServiceTokenPath -AclObject $Acl
}

function Install-SecureDesktopService {
    $Existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($Existing) {
        if ($Existing.Status -ne "Stopped") {
            Stop-Service -Name $ServiceName -Force -ErrorAction Stop
            $Existing.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(15))
        }
        & sc.exe config $ServiceName binPath= "`"$InstalledServiceExecutable`"" start= auto | Out-Null
    }
    else {
        & sc.exe create $ServiceName binPath= "`"$InstalledServiceExecutable`"" start= auto DisplayName= "Windows LAN Remote Secure Desktop" | Out-Null
    }
    if ($LASTEXITCODE -ne 0) { throw "Could not configure the secure desktop service." }
    & sc.exe description $ServiceName "Provides elevated input plus local-only access to the Windows lock and UAC secure desktop for LAN Remote." | Out-Null
    & sc.exe failure $ServiceName reset= 86400 actions= restart/3000/restart/5000/restart/10000 | Out-Null
    Start-Service -Name $ServiceName
    (Get-Service -Name $ServiceName).WaitForStatus("Running", [TimeSpan]::FromSeconds(15))
}

function Install-FirewallRules {
    foreach ($RuleName in @("WindowsLANRemote-TCP", "WindowsLANRemote-UDP")) {
        Get-NetFirewallRule -Name $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue
    }
    New-NetFirewallRule -Name "WindowsLANRemote-TCP" -DisplayName "Windows LAN Remote (TCP)" -Direction Inbound -Action Allow -Profile Private -Program $InstalledExecutable -Protocol TCP -LocalPort 8765 | Out-Null
    New-NetFirewallRule -Name "WindowsLANRemote-UDP" -DisplayName "Windows LAN Remote Discovery (UDP)" -Direction Inbound -Action Allow -Profile Private -Program $InstalledExecutable -Protocol UDP -LocalPort 8766 | Out-Null
}

if ($FunctionsOnly) { return }

try {
    Assert-Administrator
    Write-InstallLog "Starting machine-wide installation of $AppName $Version."

    $ExistingService = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($ExistingService -and $ExistingService.Status -ne "Stopped") {
        Stop-Service -Name $ServiceName -Force
        $ExistingService.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(15))
    }
    foreach ($HelperPort in @(8767, 8768)) {
        Get-NetTCPConnection -LocalPort $HelperPort -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique |
            ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
    }
    Stop-InstalledProcesses

    Reset-InstallDirectory
    New-Item -ItemType Directory -Force -Path $StartMenuDir | Out-Null

    Copy-ApplicationWithRetry -Source $SourceAppDir -Destination $InstallDir
    Copy-WithRetry -Source $SourceServiceExecutable -Destination $InstalledServiceExecutable
    Copy-WithRetry -Source (Join-Path $PSScriptRoot "README.md") -Destination (Join-Path $InstallDir "README.md")
    Copy-WithRetry -Source (Join-Path $PSScriptRoot "uninstall.cmd") -Destination (Join-Path $InstallDir "uninstall.cmd")
    Copy-WithRetry -Source (Join-Path $PSScriptRoot "uninstall.ps1") -Destination (Join-Path $InstallDir "uninstall.ps1")
    Copy-WithRetry -Source $VersionFile -Destination (Join-Path $InstallDir "VERSION.txt")

    Ensure-ServiceToken
    Install-SecureDesktopService
    Install-FirewallRules

    $Shell = New-Object -ComObject WScript.Shell
    $AppShortcut = $Shell.CreateShortcut((Join-Path $StartMenuDir "$AppName.lnk"))
    $AppShortcut.TargetPath = $InstalledExecutable
    $AppShortcut.IconLocation = "$InstalledExecutable,0"
    $AppShortcut.WorkingDirectory = $InstallDir
    $AppShortcut.Description = "Start Windows LAN Remote"
    $AppShortcut.Save()

    $RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
    $ExistingRunValue = Get-ItemProperty -Path $RunKey -Name "LAN Remote" -ErrorAction SilentlyContinue
    if ($ExistingRunValue) {
        Set-ItemProperty -Path $RunKey -Name "LAN Remote" -Value "`"$InstalledExecutable`""
    }

    $UninstallShortcut = $Shell.CreateShortcut((Join-Path $StartMenuDir "Uninstall $AppName.lnk"))
    $UninstallShortcut.TargetPath = Join-Path $InstallDir "uninstall.cmd"
    $UninstallShortcut.WorkingDirectory = $InstallDir
    $UninstallShortcut.Description = "Uninstall Windows LAN Remote"
    $UninstallShortcut.Save()

    New-Item -Force -Path $UninstallKey | Out-Null
    New-ItemProperty -Path $UninstallKey -Name "DisplayName" -Value $AppName -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $UninstallKey -Name "DisplayVersion" -Value $Version -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $UninstallKey -Name "Publisher" -Value $Publisher -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $UninstallKey -Name "InstallLocation" -Value $InstallDir -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $UninstallKey -Name "DisplayIcon" -Value $InstalledExecutable -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $UninstallKey -Name "UninstallString" -Value "`"$(Join-Path $InstallDir "uninstall.cmd")`"" -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $UninstallKey -Name "NoModify" -Value 1 -PropertyType DWord -Force | Out-Null
    New-ItemProperty -Path $UninstallKey -Name "NoRepair" -Value 1 -PropertyType DWord -Force | Out-Null

    if (Test-Path -LiteralPath $LegacyStartMenuDir) { Remove-Item -LiteralPath $LegacyStartMenuDir -Recurse -Force }
    if (Test-Path -LiteralPath $LegacyUninstallKey) { Remove-Item -LiteralPath $LegacyUninstallKey -Recurse -Force }

    Write-InstallLog "Installation completed successfully at $InstallDir."
    Write-Output "$AppName $Version installed for all users."
    Write-Output "Secure desktop service is running."
    exit 0
}
catch {
    $Details = $_ | Out-String
    Write-InstallLog "Installation failed: $Details"
    Write-Error "Installation failed. $($_.Exception.Message) Log: $LogPath"
    exit 1
}
