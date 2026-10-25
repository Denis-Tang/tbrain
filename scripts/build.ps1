# Build the Windows distribution of Tbrain.
#
# Output (dist/tbrain) matches installer.REQUIRED_FILES:
#   tbrain.exe
#   desktop/Tbrain.Desktop.exe
#   runtime/node.exe
#   _internal/                 (PyInstaller onedir payload)
#   integrations/              (DSH native plugin, pi extension)
#   README.md, LICENSE, THIRD-PARTY-NOTICES.txt
#
# The version is read from src/tbrain/__init__.py only. No network access: the
# Node license file is copied from the local Node installation when available.
#requires -Version 7.0
param([string]$OutputDirectory = 'dist')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    New-Item -ItemType Directory -Path build -Force | Out-Null
    New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null

    # --- version (single source of truth) -------------------------------
    $initFile = Join-Path $projectRoot 'src/tbrain/__init__.py'
    $versionMatch = Select-String -LiteralPath $initFile -Pattern '__version__\s*=\s*"([^"]+)"'
    if (-not $versionMatch) { throw "无法从 tbrain/__init__.py 读取版本号" }
    $version = $versionMatch.Matches[0].Groups[1].Value
    Write-Host "Tbrain version: $version"

    $python = Join-Path $projectRoot '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $python)) { $python = 'python' }

    # --- desktop host (WPF/WebView2) ------------------------------------
    dotnet publish desktop/Tbrain.Desktop.csproj -c Release -r win-x64 `
        --self-contained true -o build/desktop --nologo -v:q `
        -p:Version=$version -p:FileVersion=$version -p:InformationalVersion=$version
    if ($LASTEXITCODE -ne 0) { throw "Desktop host build failed with exit code $LASTEXITCODE" }

    # --- launcher (reconnects stdio for windowed PyInstaller) -----------
    $launcher = Join-Path $projectRoot 'build/launcher.py'
    $iconPath = Join-Path $projectRoot 'src/tbrain/web/icon.ico'
    Set-Content -LiteralPath $launcher -Encoding utf8NoBOM -Value @'
import ctypes
import msvcrt
import os
import sys

# Windowed PyInstaller sets stdio to None; reconnect inherited MCP/Hook pipes.
if getattr(sys, "frozen", False):
    get_handle = ctypes.windll.kernel32.GetStdHandle
    get_handle.restype = ctypes.c_void_p
    for name, number, mode, flags in (("stdin", -10, "r", os.O_RDONLY),
                                      ("stdout", -11, "w", os.O_WRONLY),
                                      ("stderr", -12, "w", os.O_WRONLY)):
        handle = get_handle(number)
        fd = msvcrt.open_osfhandle(handle, flags) if handle not in (None, ctypes.c_void_p(-1).value) else os.open(os.devnull, flags)
        stream = os.fdopen(fd, mode, encoding="utf-8", buffering=1)
        setattr(sys, name, stream)
        setattr(sys, "__" + name + "__", stream)

from tbrain.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
'@
    & $python -m PyInstaller --noconfirm --onedir --windowed `
        --distpath $OutputDirectory --icon $iconPath `
        --name tbrain --paths src --specpath build --workpath build/pyinstaller `
        --collect-data tbrain --hidden-import keyring.backends.Windows `
        $launcher
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

    # --- assemble the package ------------------------------------------
    $packageDirectory = Join-Path $OutputDirectory 'tbrain'
    Copy-Item -LiteralPath @('README.md', 'LICENSE', 'THIRD-PARTY-NOTICES.txt') -Destination $packageDirectory -Force
    Copy-Item -LiteralPath 'build/desktop' -Destination $packageDirectory -Recurse -Force
    Copy-Item -LiteralPath 'integrations' -Destination $packageDirectory -Recurse -Force

    $nodeExecutable = @(Get-Command node -CommandType Application)[0].Source
    $nodeVersion = & $nodeExecutable --version
    $runtimeDirectory = Join-Path $packageDirectory 'runtime'
    New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null
    Copy-Item -LiteralPath $nodeExecutable -Destination (Join-Path $runtimeDirectory 'node.exe') -Force
    $nodeLicense = Join-Path (Split-Path -Parent $nodeExecutable) 'LICENSE'
    if (Test-Path -LiteralPath $nodeLicense) {
        Copy-Item -LiteralPath $nodeLicense -Destination (Join-Path $runtimeDirectory 'NODE-LICENSE.txt') -Force
    }
    else {
        Set-Content -LiteralPath (Join-Path $runtimeDirectory 'NODE-LICENSE.txt') -Encoding utf8NoBOM -Value (
            "Node.js $nodeVersion is bundled with Tbrain. License: https://github.com/nodejs/node/blob/main/LICENSE")
    }

    # The installed package has no readable tbrain/__init__.py (PyInstaller onedir),
    # so the version is shipped as a plain file for `tbrain install`/upgrade inference.
    Set-Content -LiteralPath (Join-Path $packageDirectory 'VERSION') -Value $version `
        -Encoding utf8NoBOM -NoNewline

    # --- verify the layout the installer requires ----------------------
    $requiredFiles = @(
        'tbrain.exe',
        'VERSION',
        'desktop/Tbrain.Desktop.exe',
        'runtime/node.exe',
        'integrations/README.md',
        'integrations/tbrain-dsh.mjs',
        'integrations/pi/tbrain.ts'
    )
    foreach ($relative in $requiredFiles) {
        $path = Join-Path $packageDirectory $relative
        if (-not (Test-Path -LiteralPath $path)) { throw "构建产物缺少 $relative" }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $packageDirectory '_internal') -PathType Container)) {
        throw '构建产物缺少 _internal/ 运行时目录'
    }
    # --- release ZIP (GitHub release asset; updater expects tbrain/ at the root)
    $zipPath = Join-Path $OutputDirectory "tbrain-$version-windows-x64.zip"
    if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
    Compress-Archive -Path $packageDirectory -DestinationPath $zipPath -CompressionLevel Optimal
    $hash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLower()
    Set-Content -LiteralPath "$zipPath.sha256" -Value "$hash  $(Split-Path -Leaf $zipPath)" -Encoding ascii
    Write-Host "Release archive: $zipPath"
    Write-Host "SHA256: $hash"
    Write-Host "Built $packageDirectory (v$version)"
}
finally {
    Pop-Location
}
