$ErrorActionPreference = "Stop"

$AppName = "Windows LAN Remote"
$AppId = "WindowsLANRemote"
$Publisher = "EmpK1019"
$VersionFile = Join-Path $PSScriptRoot "VERSION.txt"
$Version = if (Test-Path -LiteralPath $VersionFile) { (Get-Content -Raw -LiteralPath $VersionFile).Trim() } else { "0.1.0" }

$InstallRoot = Join-Path $env:LOCALAPPDATA "Programs"
$InstallDir = Join-Path $InstallRoot $AppName
$StartMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$AppName"
$UninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppId"

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
New-Item -ItemType Directory -Force -Path $StartMenuDir | Out-Null

Copy-Item -LiteralPath (Join-Path $PSScriptRoot "WindowsLANRemote.exe") -Destination (Join-Path $InstallDir "WindowsLANRemote.exe") -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "README.md") -Destination (Join-Path $InstallDir "README.md") -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "uninstall.cmd") -Destination (Join-Path $InstallDir "uninstall.cmd") -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "uninstall.ps1") -Destination (Join-Path $InstallDir "uninstall.ps1") -Force
Copy-Item -LiteralPath $VersionFile -Destination (Join-Path $InstallDir "VERSION.txt") -Force

$Shell = New-Object -ComObject WScript.Shell
$AppShortcut = $Shell.CreateShortcut((Join-Path $StartMenuDir "$AppName.lnk"))
$AppShortcut.TargetPath = Join-Path $InstallDir "WindowsLANRemote.exe"
$AppShortcut.WorkingDirectory = $InstallDir
$AppShortcut.Description = "Start Windows LAN Remote"
$AppShortcut.Save()

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
New-ItemProperty -Path $UninstallKey -Name "DisplayIcon" -Value (Join-Path $InstallDir "WindowsLANRemote.exe") -PropertyType String -Force | Out-Null
New-ItemProperty -Path $UninstallKey -Name "UninstallString" -Value "`"$(Join-Path $InstallDir "uninstall.cmd")`"" -PropertyType String -Force | Out-Null
New-ItemProperty -Path $UninstallKey -Name "NoModify" -Value 1 -PropertyType DWord -Force | Out-Null
New-ItemProperty -Path $UninstallKey -Name "NoRepair" -Value 1 -PropertyType DWord -Force | Out-Null

Write-Host "$AppName $Version installed for the current user."
Write-Host "Open it from the Start menu."
