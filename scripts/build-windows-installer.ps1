param(
    [string]$Version = "1.2.1",
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
$NativeBuildDir = Join-Path $BuildDir "native"
$NativeVideoDll = Join-Path $NativeBuildDir "WindowsLANRemoteVideo.dll"
$NativeVideoEncoder = Join-Path $NativeBuildDir "WindowsLANRemoteVideoEncoder.exe"

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

& powershell.exe `
    -NoProfile `
    -NonInteractive `
    -ExecutionPolicy Bypass `
    -File (Join-Path $Root "scripts\build-native-video.ps1") `
    -OutputDir $NativeBuildDir `
    -RunTests
if ($LASTEXITCODE -ne 0) {
    throw "Native H.264 components failed to build or pass protocol tests (exit $LASTEXITCODE)."
}

$SourceText = Get-Content -Raw -Encoding UTF8 (Join-Path $Root "lan_remote.py")
$VersionMatch = [regex]::Match($SourceText, 'APP_VERSION\s*=\s*"([^"]+)"')
if (-not $VersionMatch.Success -or $VersionMatch.Groups[1].Value -ne $Version) {
    throw "Build version $Version does not match lan_remote.py APP_VERSION."
}

& $VenvPython -m py_compile (Join-Path $Root "lan_remote.py") (Join-Path $Root "tests\test_lan_remote.py")
if ($LASTEXITCODE -ne 0) {
    throw "Python syntax validation failed with exit code $LASTEXITCODE."
}
& $VenvPython -m unittest discover -s (Join-Path $Root "tests") -v
if ($LASTEXITCODE -ne 0) {
    throw "Automated tests failed with exit code $LASTEXITCODE."
}

& $VenvPython (Join-Path $Root "tests\low_latency_screen_stream_e2e.py")
if ($LASTEXITCODE -ne 0) {
    throw "Low-latency screen stream failed the 33 FPS release gate with exit code $LASTEXITCODE."
}

foreach ($NativeFps in @(30, 60, 120)) {
    & $VenvPython `
        (Join-Path $Root "tests\native_video_pipeline_e2e.py") `
        --fps $NativeFps `
        --measure-seconds 3 `
        --native-dir $NativeBuildDir `
        --enforce-performance
    if ($LASTEXITCODE -ne 0) {
        throw "Native H.264 $NativeFps FPS end-to-end gate failed with exit code $LASTEXITCODE."
    }
}

& $VenvPython `
    (Join-Path $Root "tests\native_video_pipeline_e2e.py") `
    --fps 30 `
    --measure-seconds 1 `
    --native-dir $NativeBuildDir `
    --exercise-secure-transition
if ($LASTEXITCODE -ne 0) {
    throw "Native secure-desktop fallback/recovery E2E failed with exit code $LASTEXITCODE."
}

& powershell.exe `
    -NoProfile `
    -NonInteractive `
    -ExecutionPolicy Bypass `
    -File (Join-Path $Root "tests\InstallProcessSelectionTests.ps1") `
    -InstallScript (Join-Path $PackagingDir "install.ps1")
if ($LASTEXITCODE -ne 0) {
    throw "Installer process-selection tests failed with exit code $LASTEXITCODE."
}

if (Test-Path -LiteralPath $StageDir) {
    Remove-Item -LiteralPath $StageDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $StageDir | Out-Null
New-Item -ItemType Directory -Force -Path $DistDir | Out-Null
New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null
Get-ChildItem -LiteralPath $DistDir -Filter "~WindowsLANRemoteSetup*.CAB" -ErrorAction SilentlyContinue | Remove-Item -Force

# Windows refuses to remove a directory that is another process's current
# working directory. Upgrades therefore clear its contents and reuse it.
$DirectoryProbe = Join-Path $BuildDir "directory-in-use-probe"
if (Test-Path -LiteralPath $DirectoryProbe) {
    Remove-Item -LiteralPath $DirectoryProbe -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $DirectoryProbe | Out-Null
Set-Content -LiteralPath (Join-Path $DirectoryProbe "old-version.txt") -Value "old" -Encoding ASCII
$DirectoryHolder = Start-Process `
    -FilePath "powershell.exe" `
    -ArgumentList '-NoProfile -NonInteractive -Command "Start-Sleep -Seconds 20"' `
    -WorkingDirectory $DirectoryProbe `
    -WindowStyle Hidden `
    -PassThru
try {
    Start-Sleep -Milliseconds 400
    Get-ChildItem -LiteralPath $DirectoryProbe -Force | Remove-Item -Recurse -Force
    if ((Get-ChildItem -LiteralPath $DirectoryProbe -Force | Measure-Object).Count -ne 0) {
        throw "Directory-in-use upgrade regression test failed."
    }
}
finally {
    Stop-Process -Id $DirectoryHolder.Id -Force -ErrorAction SilentlyContinue
    Wait-Process -Id $DirectoryHolder.Id -Timeout 5 -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $DirectoryProbe -Recurse -Force -ErrorAction SilentlyContinue
}

& $VenvPython -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --windowed `
    --name $PortableBaseName `
    --add-data "$((Join-Path $Root 'web'));web" `
    --add-data "$((Join-Path $Root 'assets'));assets" `
    --collect-all dxcam `
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

Copy-Item -LiteralPath $NativeVideoDll -Destination $PortableDir -Force
Copy-Item -LiteralPath $NativeVideoEncoder -Destination $PortableDir -Force

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
    "/platform:x64",
    "/out:$ControlHostPath",
    "/win32icon:$IconPath",
    "/reference:System.Drawing.dll",
    "/reference:System.Web.Extensions.dll",
    "/reference:System.Web.dll",
    "/reference:System.Windows.Forms.dll",
    "/reference:$((Join-Path $WebViewLibDir 'Microsoft.Web.WebView2.Core.dll'))",
    "/reference:$((Join-Path $WebViewLibDir 'Microsoft.Web.WebView2.WinForms.dll'))",
    (Join-Path $PackagingDir "ControlWindowHost.cs")
)
& $CscPath @ControlHostCompilerArgs
if ($LASTEXITCODE -ne 0) {
    throw "Native control window host compilation failed with exit code $LASTEXITCODE."
}
if (-not (Test-Path -LiteralPath $ControlHostPath)) {
    throw "Native control window host was not created at $ControlHostPath"
}

Copy-Item -LiteralPath (Join-Path $WebViewLibDir "Microsoft.Web.WebView2.Core.dll") -Destination $PortableDir -Force
Copy-Item -LiteralPath (Join-Path $WebViewLibDir "Microsoft.Web.WebView2.WinForms.dll") -Destination $PortableDir -Force
Copy-Item -LiteralPath (Join-Path $WebViewLibDir "runtimes\win-x64\native\WebView2Loader.dll") -Destination $PortableDir -Force

$ControlHostTestPath = Join-Path $BuildDir "ControlWindowHostTests.exe"
& $CscPath `
    /nologo `
    /target:exe `
    "/out:$ControlHostTestPath" `
    /reference:System.Drawing.dll `
    /reference:System.Windows.Forms.dll `
    (Join-Path $Root "tests\ControlWindowHostTests.cs")
if ($LASTEXITCODE -ne 0) {
    throw "Control window host test compilation failed with exit code $LASTEXITCODE."
}
& $ControlHostTestPath $ControlHostPath
if ($LASTEXITCODE -ne 0) {
    throw "Control window host state tests failed with exit code $LASTEXITCODE."
}

& $VenvPython `
    (Join-Path $Root "tests\native_video_pipeline_e2e.py") `
    --fps 60 `
    --measure-seconds 3 `
    --native-dir $PortableDir `
    --enforce-performance
if ($LASTEXITCODE -ne 0) {
    throw "Packaged native H.264 pipeline failed with exit code $LASTEXITCODE."
}

& $VenvPython `
    (Join-Path $Root "tests\packaged_native_video_host_e2e.py") `
    --control-host $ControlHostPath `
    --native-dir $PortableDir
if ($LASTEXITCODE -ne 0) {
    throw "Packaged WebView2/native H.264 host E2E failed with exit code $LASTEXITCODE."
}

foreach ($InteractiveTest in @(
    "PackagedKeyboardE2ETests",
    "PackagedMouseE2ETests",
    "PackagedMouseHookE2ETests"
)) {
    $InteractiveTestPath = Join-Path $BuildDir "$InteractiveTest.exe"
    & $CscPath `
        /nologo `
        /target:exe `
        "/out:$InteractiveTestPath" `
        /reference:System.Drawing.dll `
        /reference:System.Windows.Forms.dll `
        (Join-Path $Root "tests\$InteractiveTest.cs")
    if ($LASTEXITCODE -ne 0) {
        throw "$InteractiveTest compilation failed with exit code $LASTEXITCODE."
    }
}

$MouseHookTestPath = Join-Path $BuildDir "PackagedMouseHookE2ETests.exe"
& $MouseHookTestPath $ControlHostPath
if ($LASTEXITCODE -ne 0) {
    throw "Packaged native mouse hook end-to-end test failed with exit code $LASTEXITCODE."
}

$PreviousControllerPort = $env:LAN_REMOTE_CONTROLLER_PORT
try {
    $env:LAN_REMOTE_CONTROLLER_PORT = "0"
    & $VenvPython (Join-Path $Root "tests\full_mouse_pipeline_e2e.py")
    if ($LASTEXITCODE -ne 0) {
        throw "Full mouse page/bridge/hook/stream end-to-end test failed with exit code $LASTEXITCODE."
    }
}
finally {
    $env:LAN_REMOTE_CONTROLLER_PORT = $PreviousControllerPort
}

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

$LaunchProbe = Start-Process -FilePath $InstallerPath -ArgumentList "--launch-probe" -WindowStyle Hidden -Wait -PassThru
if ($LaunchProbe.ExitCode -ne 0) {
    throw "Installer launch probe failed with exit code $($LaunchProbe.ExitCode)."
}

$RestartProbeDir = Join-Path $BuildDir "installer-restart-probe"
if (Test-Path -LiteralPath $RestartProbeDir) {
    Remove-Item -LiteralPath $RestartProbeDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $RestartProbeDir | Out-Null
Set-Content -LiteralPath (Join-Path $RestartProbeDir "VERSION.txt") -Value $Version -NoNewline -Encoding ASCII
New-Item -ItemType File -Force -Path (Join-Path $RestartProbeDir "WindowsLANRemote-$Version.exe") | Out-Null
try {
    $RestartPathProbe = Start-Process -FilePath $InstallerPath -ArgumentList "--restart-path-probe `"$RestartProbeDir`"" -WindowStyle Hidden -Wait -PassThru
    if ($RestartPathProbe.ExitCode -ne 0) {
        throw "Installer restart path probe failed with exit code $($RestartPathProbe.ExitCode)."
    }
}
finally {
    Remove-Item -LiteralPath $RestartProbeDir -Recurse -Force -ErrorAction SilentlyContinue
}

$RequiredPortableFiles = @(
    $PortableExecutable,
    $ControlHostPath,
    (Join-Path $PortableDir "Microsoft.Web.WebView2.Core.dll"),
    (Join-Path $PortableDir "Microsoft.Web.WebView2.WinForms.dll"),
    (Join-Path $PortableDir "WebView2Loader.dll"),
    (Join-Path $PortableDir "WindowsLANRemoteVideo.dll"),
    (Join-Path $PortableDir "WindowsLANRemoteVideoEncoder.exe")
)
foreach ($RequiredFile in $RequiredPortableFiles) {
    if (-not (Test-Path -LiteralPath $RequiredFile)) {
        throw "Build output is incomplete: $RequiredFile"
    }
}

Write-Host "Built:"
Write-Host "  $PortableArchive"
Write-Host "  $ServicePath"
Write-Host "  $InstallerPath"
Get-FileHash -Algorithm SHA256 $PortableArchive, $ServicePath, $InstallerPath |
    ForEach-Object { Write-Host "  SHA256 $([IO.Path]::GetFileName($_.Path)) $($_.Hash)" }
