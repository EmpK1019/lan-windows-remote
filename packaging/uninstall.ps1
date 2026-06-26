$ErrorActionPreference = "Stop"

$AppName = "Windows LAN Remote"
$AppId = "WindowsLANRemote"
$InstallDir = Join-Path $env:LOCALAPPDATA "Programs\$AppName"
$StartMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$AppName"
$UninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppId"

if (Test-Path -LiteralPath $StartMenuDir) {
    Remove-Item -LiteralPath $StartMenuDir -Recurse -Force
}

if (Test-Path -LiteralPath $UninstallKey) {
    Remove-Item -LiteralPath $UninstallKey -Recurse -Force
}

if (Test-Path -LiteralPath $InstallDir) {
    $CleanupScript = Join-Path $env:TEMP ("WindowsLANRemote-cleanup-{0}.cmd" -f $PID)
    $CleanupContent = @"
@echo off
timeout /t 2 /nobreak >nul
rmdir /s /q "$InstallDir" >nul 2>nul
del "%~f0" >nul 2>nul
"@
    Set-Content -LiteralPath $CleanupScript -Value $CleanupContent -Encoding ASCII
    Start-Process -FilePath $env:ComSpec -ArgumentList "/c", "`"$CleanupScript`"" -WindowStyle Hidden | Out-Null
}

Write-Host "$AppName was uninstalled."
