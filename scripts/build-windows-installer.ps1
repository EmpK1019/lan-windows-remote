param(
    [string]$Version = "0.6.4",
    [switch]$SkipDependencyInstall
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
$PackagingDir = Join-Path $Root "packaging"
$BuildDir = Join-Path $Root "build"
$DistDir = Join-Path $Root "dist"
$StageDir = Join-Path $BuildDir "installer-stage"
$PayloadZip = Join-Path $BuildDir "WindowsLANRemotePayload-$Version.zip"
$VenvPython = Join-Path $Root ".venv-build\Scripts\python.exe"
$InstallerPath = Join-Path $DistDir "WindowsLANRemoteSetup-$Version.exe"
$PortableBaseName = "WindowsLANRemote-$Version"
$PortableDir = Join-Path $DistDir $PortableBaseName
$PortableExecutable = Join-Path $PortableDir "$PortableBaseName.exe"
$PortableArchive = Join-Path $DistDir "$PortableBaseName-portable.zip"
$ServicePath = Join-Path $DistDir "WindowsLANRemoteService-$Version.exe"
$IconPath = Join-Path $Root "assets\lan-remote-icon.ico"
$WebViewLibDir = Join-Path $Root ".venv-build\Lib\site-packages\webview\lib"
$ControlHostPath = Join-Path $PortableDir "WindowsLANRemoteControlHost.exe"

function Assert-Tool {
    param([string]$Name)
    $Path = (Get-Command $Name -ErrorAction SilentlyContinue)
    if (-not $Path) {
        throw "Required tool '$Name' was not found on PATH."
    }
}

Set-Location $Root
Assert-Tool "python"

$CscCandidates = @(
    (Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"),
    (Join-Path $env:WINDIR "Microsoft.NET\Framework\v4.0.30319\csc.exe")
)
$CscPath = $CscCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $CscPath) {
    throw "Required .NET Framework C# compiler was not found."
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    python -m venv (Join-Path $Root ".venv-build")
}

if (-not $SkipDependencyInstall) {
    & $VenvPython -m pip install -r (Join-Path $PackagingDir "requirements-build.txt")
}

if (Test-Path -LiteralPath $StageDir) {
    Remove-Item -LiteralPath $StageDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $StageDir | Out-Null
New-Item -ItemType Directory -Force -Path $DistDir | Out-Null
New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null
Get-ChildItem -LiteralPath $DistDir -Filter "~WindowsLANRemoteSetup*.CAB" -ErrorAction SilentlyContinue | Remove-Item -Force

& $VenvPython -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --windowed `
    --name $PortableBaseName `
    --add-data "$((Join-Path $Root 'web'));web" `
    --add-data "$((Join-Path $Root 'assets'));assets" `
    --icon $IconPath `
    --distpath $DistDir `
    --workpath (Join-Path $BuildDir "pyinstaller") `
    --specpath (Join-Path $BuildDir "pyinstaller-spec") `
    (Join-Path $Root "lan_remote.py")
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE."
}
if (-not (Test-Path -LiteralPath $PortableExecutable)) {
    throw "Portable application directory was not created at $PortableDir"
}

& $CscPath `
    /nologo `
    /target:winexe `
    "/out:$ServicePath" `
    "/win32icon:$IconPath" `
    /reference:System.ServiceProcess.dll `
    (Join-Path $PackagingDir "SecureDesktopService.cs")
if ($LASTEXITCODE -ne 0) {
    throw "Secure desktop service compilation failed with exit code $LASTEXITCODE."
}

$ControlHostCompilerArgs = @(
    "/nologo",
    "/target:winexe",
    "/out:$ControlHostPath",
    "/win32icon:$IconPath",
    "/reference:System.Drawing.dll",
    "/reference:System.Web.Extensions.dll",
    "/reference:System.Windows.Forms.dll",
    "/reference:$((Join-Path $WebViewLibDir 'Microsoft.Web.WebView2.Core.dll'))",
    "/reference:$((Join-Path $WebViewLibDir 'Microsoft.Web.WebView2.WinForms.dll'))",
    (Join-Path $PackagingDir "ControlWindowHost.cs")
)
& $CscPath @ControlHostCompilerArgs
if ($LASTEXITCODE -ne 0) {
    throw "Native control window host compilation failed with exit code $LASTEXITCODE."
}

Copy-Item -LiteralPath (Join-Path $WebViewLibDir "Microsoft.Web.WebView2.Core.dll") -Destination $PortableDir -Force
Copy-Item -LiteralPath (Join-Path $WebViewLibDir "Microsoft.Web.WebView2.WinForms.dll") -Destination $PortableDir -Force
Copy-Item -LiteralPath (Join-Path $WebViewLibDir "runtimes\win-x64\native\WebView2Loader.dll") -Destination $PortableDir -Force

$StagedAppDir = Join-Path $StageDir "app"
Copy-Item -LiteralPath $PortableDir -Destination $StagedAppDir -Recurse -Force
Copy-Item -LiteralPath $ServicePath -Destination (Join-Path $StageDir "WindowsLANRemoteService.exe") -Force
Copy-Item -LiteralPath (Join-Path $Root "README.md") -Destination (Join-Path $StageDir "README.md") -Force
Copy-Item -LiteralPath (Join-Path $PackagingDir "install.cmd") -Destination (Join-Path $StageDir "install.cmd") -Force
Copy-Item -LiteralPath (Join-Path $PackagingDir "install.ps1") -Destination (Join-Path $StageDir "install.ps1") -Force
Copy-Item -LiteralPath (Join-Path $PackagingDir "uninstall.cmd") -Destination (Join-Path $StageDir "uninstall.cmd") -Force
Copy-Item -LiteralPath (Join-Path $PackagingDir "uninstall.ps1") -Destination (Join-Path $StageDir "uninstall.ps1") -Force
Copy-Item -LiteralPath (Join-Path $PackagingDir "license.txt") -Destination (Join-Path $StageDir "license.txt") -Force
Set-Content -LiteralPath (Join-Path $StageDir "VERSION.txt") -Value $Version -NoNewline -Encoding ASCII

if (Test-Path -LiteralPath $InstallerPath) {
    Remove-Item -LiteralPath $InstallerPath -Force
}

if (Test-Path -LiteralPath $PayloadZip) {
    Remove-Item -LiteralPath $PayloadZip -Force
}

if (Test-Path -LiteralPath $PortableArchive) {
    Remove-Item -LiteralPath $PortableArchive -Force
}

# Keep Python.NET/WebView2 in an installed directory. PyInstaller's one-file
# bootloader can permanently stall the WinForms message loop after startup.
# Users still launch one normal EXE; the runtime files stay beside it.
Compress-Archive -Path (Join-Path $PortableDir "*") -DestinationPath $PortableArchive -Force
Compress-Archive -Path (Join-Path $StageDir "*") -DestinationPath $PayloadZip -Force

$CompilerArgs = @(
    "/nologo",
    "/target:winexe",
    "/out:$InstallerPath",
    "/win32icon:$IconPath",
    "/win32manifest:$((Join-Path $PackagingDir 'setup.manifest'))",
    "/resource:$PayloadZip,Payload.zip",
    "/reference:System.Windows.Forms.dll",
    "/reference:System.IO.Compression.dll",
    "/reference:System.IO.Compression.FileSystem.dll",
    (Join-Path $PackagingDir "SetupBootstrapper.cs")
)

& $CscPath @CompilerArgs
if ($LASTEXITCODE -ne 0) {
    throw "C# installer bootstrapper compilation failed with exit code $LASTEXITCODE."
}

if (-not (Test-Path -LiteralPath $InstallerPath)) {
    throw "Installer was not created at $InstallerPath"
}

Write-Host "Built:"
Write-Host "  $PortableArchive"
Write-Host "  $ServicePath"
Write-Host "  $InstallerPath"
