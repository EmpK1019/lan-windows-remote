param(
    [string]$OutputDir = "",
    [switch]$RunTests
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
if (-not $OutputDir) {
    $OutputDir = Join-Path $Root "build\native"
}
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$VsWhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path -LiteralPath $VsWhere)) {
    throw "Visual Studio Build Tools detection failed: vswhere.exe is missing. Install the C++ build tools workload."
}
$VsRoot = & $VsWhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $VsRoot) {
    throw "Visual Studio C++ x64 build tools are missing. Install Microsoft.VisualStudio.Workload.VCTools."
}
$MsvcRoot = Get-ChildItem -LiteralPath (Join-Path $VsRoot "VC\Tools\MSVC") -Directory |
    Sort-Object Name -Descending |
    Select-Object -First 1 -ExpandProperty FullName
if (-not $MsvcRoot) {
    throw "The MSVC compiler directory could not be located under $VsRoot."
}
$Compiler = Join-Path $MsvcRoot "bin\Hostx64\x64\cl.exe"
if (-not (Test-Path -LiteralPath $Compiler)) {
    throw "The x64 MSVC compiler is missing at $Compiler."
}

$SdkRoot = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10"
$SdkVersion = Get-ChildItem -LiteralPath (Join-Path $SdkRoot "Include") -Directory |
    Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "um\mfapi.h") } |
    Sort-Object Name -Descending |
    Select-Object -First 1 -ExpandProperty Name
if (-not $SdkVersion) {
    throw "Windows 10/11 SDK with Media Foundation headers is missing. Install a Windows SDK component."
}

$PreviousInclude = $env:INCLUDE
$PreviousLib = $env:LIB
try {
    $env:INCLUDE = @(
        (Join-Path $MsvcRoot "include"),
        (Join-Path $SdkRoot "Include\$SdkVersion\ucrt"),
        (Join-Path $SdkRoot "Include\$SdkVersion\shared"),
        (Join-Path $SdkRoot "Include\$SdkVersion\um"),
        (Join-Path $SdkRoot "Include\$SdkVersion\winrt")
    ) -join ";"
    $env:LIB = @(
        (Join-Path $MsvcRoot "lib\x64"),
        (Join-Path $SdkRoot "Lib\$SdkVersion\ucrt\x64"),
        (Join-Path $SdkRoot "Lib\$SdkVersion\um\x64")
    ) -join ";"

    $Common = @("/nologo", "/std:c++17", "/EHsc", "/O2", "/W4", "/WX", "/DUNICODE", "/D_UNICODE")
    & $Compiler @Common `
        "/Fe:$OutputDir\WindowsLANRemoteVideoEncoder.exe" `
        "/Fo:$OutputDir\Encoder.obj" `
        (Join-Path $Root "native\WindowsLANRemoteVideoEncoder.cpp") `
        /link d3d11.lib dxgi.lib mfplat.lib mf.lib mfuuid.lib ole32.lib oleaut32.lib user32.lib gdi32.lib winmm.lib
    if ($LASTEXITCODE -ne 0) {
        throw "Native H.264 encoder compilation failed with exit code $LASTEXITCODE."
    }

    & $Compiler @Common /LD `
        "/Fe:$OutputDir\WindowsLANRemoteVideo.dll" `
        "/Fo:$OutputDir\Video.obj" `
        (Join-Path $Root "native\WindowsLANRemoteVideo.cpp") `
        /link d3d11.lib dxgi.lib mfplat.lib mf.lib mfuuid.lib ole32.lib oleaut32.lib ws2_32.lib user32.lib gdi32.lib
    if ($LASTEXITCODE -ne 0) {
        throw "Native H.264 decoder/renderer compilation failed with exit code $LASTEXITCODE."
    }

    if ($RunTests) {
        $ProtocolTest = Join-Path $OutputDir "NativeVideoProtocolTests.exe"
        & $Compiler @Common `
            "/Fe:$ProtocolTest" `
            "/Fo:$OutputDir\ProtocolTests.obj" `
            (Join-Path $Root "tests\NativeVideoProtocolTests.cpp")
        if ($LASTEXITCODE -ne 0) {
            throw "Native video protocol test compilation failed with exit code $LASTEXITCODE."
        }
        & $ProtocolTest
        if ($LASTEXITCODE -ne 0) {
            throw "Native video protocol tests failed with exit code $LASTEXITCODE."
        }
    }
}
finally {
    $env:INCLUDE = $PreviousInclude
    $env:LIB = $PreviousLib
}

foreach ($Required in @(
    (Join-Path $OutputDir "WindowsLANRemoteVideoEncoder.exe"),
    (Join-Path $OutputDir "WindowsLANRemoteVideo.dll")
)) {
    if (-not (Test-Path -LiteralPath $Required)) {
        throw "Native video build output is missing: $Required"
    }
}

Write-Host "Native video components built in $OutputDir"
