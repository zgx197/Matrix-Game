#!/usr/bin/env pwsh
<#
.SYNOPSIS
Build the Windows (D3D11) game entry next to the Linux/macOS one.

.DESCRIPTION
Mirrors scripts/build_game.sh for Windows: configure with CMake, build the
Release configuration and leave MedievalCastleBBoxMap.exe in game/bin.

The Windows entry only needs MSVC and the Windows SDK. SDL2, OpenGL, GLib and
FFmpeg development libraries are POSIX-build requirements and are not needed
here; FFmpeg is still required at runtime for --record.

.PARAMETER Configuration
CMake configuration to build. Defaults to Release.

.PARAMETER Jobs
Parallel build jobs. Defaults to the processor count.

.PARAMETER Clean
Remove the build directory before configuring.

.PARAMETER CMakeArgs
Extra arguments forwarded to the configure step.

.EXAMPLE
pwsh -File scripts/build_game.ps1
.EXAMPLE
pwsh -File scripts/build_game.ps1 -Clean -Configuration RelWithDebInfo
#>
[CmdletBinding()]
param(
    [ValidateSet('Debug', 'Release', 'RelWithDebInfo', 'MinSizeRel')]
    [string]$Configuration = 'Release',
    [int]$Jobs = 0,
    [switch]$Clean,
    [string[]]$CMakeArgs = @()
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$game = Join-Path $root 'game'
$build = Join-Path $game 'build'
$bin = Join-Path $game 'bin'

if (-not (Test-Path (Join-Path $game 'CMakeLists.txt'))) {
    throw "Cannot find game/CMakeLists.txt next to $PSScriptRoot"
}
if ($Jobs -le 0) { $Jobs = [Environment]::ProcessorCount }

function Find-VSWhere {
    $candidates = @(
        "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe",
        "$env:ProgramFiles\Microsoft Visual Studio\Installer\vswhere.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

$vswhere = Find-VSWhere

function Find-CMake {
    $onPath = Get-Command cmake -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    if (-not $vswhere) { return $null }
    # Visual Studio ships CMake next to MSBuild; use it when PATH has none.
    $installs = & $vswhere -products * -property installationPath 2>$null
    foreach ($install in $installs) {
        $candidate = Join-Path $install 'Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe'
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

$cmake = Find-CMake
if (-not $cmake) {
    throw 'CMake was not found. Install CMake 3.20 or later, or install Visual Studio with the C++ workload.'
}

$generatorArgs = @('-A', 'x64')
if ($vswhere) {
    $instance = (& $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null | Select-Object -First 1)
    if (-not $instance) {
        throw 'No Visual Studio installation with the C++ x64 toolset was found.'
    }
    $generatorArgs += @('-G', 'Visual Studio 17 2022', "-DCMAKE_GENERATOR_INSTANCE=$instance")
} else {
    $generatorArgs += @('-G', 'Ninja')
}

# MSBuild copies the parent environment into the compiler process. Windows
# environment names are case-insensitive, so having both HTTP_PROXY and
# http_proxy set (a common result of mixing shell and system proxy settings)
# makes that copy throw ArgumentException and the compiler is reported as
# missing. Drop the lower-case duplicates for this process and its children.
foreach ($name in 'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy') {
    if (Test-Path "Env:$name") { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
}

if ($Clean -and (Test-Path $build)) {
    Remove-Item $build -Recurse -Force
}

Write-Host "CMake: $cmake"
Write-Host "Configuring $game"
& $cmake -S $game -B $build @generatorArgs @CMakeArgs
if ($LASTEXITCODE -ne 0) { throw "CMake configure failed ($LASTEXITCODE)" }

Write-Host "Building $Configuration with $Jobs jobs"
& $cmake --build $build --config $Configuration --parallel $Jobs
if ($LASTEXITCODE -ne 0) { throw "CMake build failed ($LASTEXITCODE)" }

$executable = Join-Path $bin 'MedievalCastleBBoxMap.exe'
if (-not (Test-Path $executable)) { throw "Build succeeded but $executable is missing" }
Write-Host ''
Write-Host "Built $executable"
Write-Host ''
Write-Host 'Run it with an asset directory:'
Write-Host "  `$env:MATRIX_GAME_DATA_DIR = '$root\resources\game'"
Write-Host "  $executable --scene ancient-egypt"
Write-Host ''
Write-Host 'Place ffmpeg.exe in game/bin (or on PATH) to use --record.'
