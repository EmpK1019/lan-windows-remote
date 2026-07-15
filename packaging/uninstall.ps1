$ErrorActionPreference = "Stop"

$AppName = "Windows LAN Remote"
$AppId = "WindowsLANRemote"
$ServiceName = "WindowsLANRemoteSecureDesktop"
$InstallDir = Join-Path $env:ProgramFiles $AppName
$LegacyInstallDir = Join-Path $env:LOCALAPPDATA "Programs\$AppName"
$StartMenuDir = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\$AppName"
$LegacyStartMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$AppName"
$UninstallKey = "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppId"
$LegacyUninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppId"
$ServiceDataDir = Join-Path $env:ProgramData $AppName

function Test-Administrator {
    $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $Principal = New-Object Security.Principal.WindowsPrincipal($Identity)
    return $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Administrator)) {
    $Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    $Process = Start-Process -FilePath "powershell.exe" -ArgumentList $Arguments -Verb RunAs -Wait -PassThru
    exit $Process.ExitCode
}

$Service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($Service) {
    if ($Service.Status -ne "Stopped") {
        Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
        try { $Service.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(15)) } catch { }
    }
    & sc.exe delete $ServiceName | Out-Null
}
Get-NetTCPConnection -LocalPort 8767 -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique |
    ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }

Get-Process -ErrorAction SilentlyContinue | ForEach-Object {
    try {
        $ProcessPath = $_.Path
        if ($ProcessPath -and ($ProcessPath.StartsWith($InstallDir, [StringComparison]::OrdinalIgnoreCase) -or $ProcessPath.StartsWith($LegacyInstallDir, [StringComparison]::OrdinalIgnoreCase))) {
            Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        }
    }
    catch { }
}

foreach ($RuleName in @("WindowsLANRemote-TCP", "WindowsLANRemote-UDP")) {
    Get-NetFirewallRule -Name $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue
}

foreach ($Path in @($StartMenuDir, $LegacyStartMenuDir, $ServiceDataDir)) {
    if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Recurse -Force }
}
foreach ($RegistryPath in @($UninstallKey, $LegacyUninstallKey)) {
    if (Test-Path -LiteralPath $RegistryPath) { Remove-Item -LiteralPath $RegistryPath -Recurse -Force }
}
Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "LAN Remote" -ErrorAction SilentlyContinue

$CleanupScript = Join-Path $env:TEMP ("WindowsLANRemote-cleanup-{0}.ps1" -f $PID)
$EscapedInstallDir = $InstallDir.Replace("'", "''")
$EscapedLegacyDir = $LegacyInstallDir.Replace("'", "''")
$CleanupContent = @"
Start-Sleep -Seconds 3
Remove-Item -LiteralPath '$EscapedInstallDir' -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath '$EscapedLegacyDir' -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath `$PSCommandPath -Force -ErrorAction SilentlyContinue
"@
Set-Content -LiteralPath $CleanupScript -Value $CleanupContent -Encoding UTF8
Start-Process -FilePath "powershell.exe" -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$CleanupScript`"" -WindowStyle Hidden | Out-Null

Write-Host "$AppName was uninstalled."
