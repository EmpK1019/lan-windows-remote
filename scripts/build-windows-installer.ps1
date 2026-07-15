param(
    [string]$Version = "0.5.0",
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
$PortablePath = Join-Path $DistDir "$PortableBaseName.exe"
$ServicePath = Join-Path $DistDir "WindowsLANRemoteService-$Version.exe"
$IconPath = Join-Path $Root "assets\lan-remote-icon.ico"

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
    --onefile `
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

Copy-Item -LiteralPath $PortablePath -Destination (Join-Path $StageDir "WindowsLANRemote.exe") -Force
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

Compress-Archive -LiteralPath (Join-Path $StageDir "WindowsLANRemote.exe"), `
    (Join-Path $StageDir "WindowsLANRemoteService.exe"), `
    (Join-Path $StageDir "README.md"), `
    (Join-Path $StageDir "install.cmd"), `
    (Join-Path $StageDir "install.ps1"), `
    (Join-Path $StageDir "uninstall.cmd"), `
    (Join-Path $StageDir "uninstall.ps1"), `
    (Join-Path $StageDir "license.txt"), `
    (Join-Path $StageDir "VERSION.txt") `
    -DestinationPath $PayloadZip `
    -Force

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
Write-Host "  $PortablePath"
Write-Host "  $ServicePath"
Write-Host "  $InstallerPath"
