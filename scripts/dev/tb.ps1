#requires -Version 7.0
param(
    [ValidateSet('installed', 'dev', 'rc')][string]$Target = 'dev',
    [string]$AppHome = (Join-Path $env:TEMP 'tbrain-dev-home'),
    [string]$Vault = (Join-Path $env:TEMP 'tbrain-dev-vault'),
    [string]$Root = '',
    [string]$Label = 'guard',
    [string]$InputJson = '',
    [Parameter(ValueFromRemainingArguments)][string[]]$CommandArgs = @('version')
)
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion.Major -lt 7) { throw 'Use PowerShell 7.' }
$utf8 = [Text.UTF8Encoding]::new($false, $true)
$tempRoot = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\', '/')
function Temp-Path([string]$Value) {
    $full = [IO.Path]::GetFullPath($Value)
    if (-not $full.StartsWith($tempRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path must be below TEMP: $full"
    }
    return $full
}
$AppHome = Temp-Path $AppHome
$Vault = Temp-Path $Vault
if (-not $Root) { $Root = Join-Path $tempRoot 'tbrain-dev-install' }
$Root = Temp-Path $Root
if ($Target -eq 'rc' -and ((Split-Path $Root -Leaf) -notlike 'tbrain-rc-*' -or -not (Test-Path -LiteralPath $Root -PathType Container))) {
    throw 'rc requires an existing TEMP/tbrain-rc-* installation root.'
}
if ($CommandArgs -contains '--home' -or $CommandArgs -contains '--root' -or ($CommandArgs | Where-Object { $_ -match '^--(?:home|root)=' })) {
    throw 'Use -AppHome and -Root for wrapper-owned paths.'
}
if ($Target -eq 'installed' -and $CommandArgs[0] -in @('install', 'upgrade', 'rollback', 'uninstall', 'evict', 'connect', 'disconnect')) {
    throw 'installed permits reproduction only, not installation or host changes.'
}
if ($CommandArgs[0] -eq 'project') {
    for ($i = 0; $i -lt $CommandArgs.Count; $i++) {
        if ($CommandArgs[$i] -eq '--path') { [void](Temp-Path $CommandArgs[$i + 1]) }
        if ($CommandArgs[$i] -like '--path=*') { [void](Temp-Path $CommandArgs[$i].Substring(7)) }
    }
}
$entry = switch ($Target) {
    installed { Join-Path $env:LOCALAPPDATA 'Programs/Tbrain/bin/tbrain.cmd' }
    dev { Join-Path $tempRoot 'tbrain-dev-venv/Scripts/tbrain.exe' }
    rc { Join-Path $Root 'bin/tbrain.cmd' }
}
if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) { throw "Entry not found: $entry" }
$envPaths = @{
    TBRAIN_HOME = $AppHome
    TBRAIN_INSTALL_ROOT = $Root
    TBRAIN_SHORTCUT_DIRS = (Join-Path $tempRoot 'tbrain-shortcuts')
    CODEX_HOME = (Join-Path $tempRoot 'tbrain-dev-host/codex')
    PI_CODING_AGENT_DIR = (Join-Path $tempRoot 'tbrain-dev-host/pi')
    CLAUDE_CONFIG_DIR = (Join-Path $tempRoot 'tbrain-dev-host/claude')
    DSH_HOME = (Join-Path $tempRoot 'tbrain-dev-host/dsh')
    USERPROFILE = (Join-Path $tempRoot 'tbrain-dev-userhome')
    HOME = (Join-Path $tempRoot 'tbrain-dev-userhome')
}
foreach ($path in @($envPaths.Values) + @($Vault)) { [void][IO.Directory]::CreateDirectory($path) }
if (Get-ChildItem -LiteralPath $envPaths.TBRAIN_SHORTCUT_DIRS) { throw 'Shortcut test directory must be empty.' }
$settingsPath = Join-Path $AppHome 'settings.json'
if (-not (Test-Path -LiteralPath $settingsPath)) {
    $settings = @{ vault_path = $Vault; timezone = 'Asia/Shanghai'; ntp_enabled = $false; timeout_hours = 0;
        reminder_context_switch = $true; install_root = $Root }
    [IO.File]::WriteAllText($settingsPath, ($settings | ConvertTo-Json), $utf8)
} else {
    $settings = [IO.File]::ReadAllText($settingsPath, $utf8) | ConvertFrom-Json
    if ([IO.Path]::GetFullPath($settings.vault_path) -ne $Vault) { throw 'settings.json vault_path differs from -Vault.' }
}
$logDir = Join-Path $tempRoot 'tbrain-dev-log'
[void][IO.Directory]::CreateDirectory($logDir)
if ($Label -match '[\\/:*?"<>|]') { throw 'Label must be a filename prefix.' }
$logPath = Join-Path $logDir "$Label-$([DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss-fffffff')).jsonl"
function Invoke-Tbrain([string[]]$Arguments, [string]$Payload = '') {
    $psi = [Diagnostics.ProcessStartInfo]::new()
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.WorkingDirectory = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
    if ($entry.EndsWith('.cmd')) {
        $psi.FileName = $env:ComSpec
        $quoted = ($Arguments | ForEach-Object { '"' + $_.Replace('"', '""') + '"' }) -join ' '
        $psi.Arguments = '/d /s /c ""' + $entry + '" ' + $quoted + '"'
    } else {
        $psi.FileName = $entry
        foreach ($argument in $Arguments) { $psi.ArgumentList.Add($argument) }
    }
    foreach ($key in $envPaths.Keys) { $psi.Environment[$key] = $envPaths[$key] }
    $psi.Environment['PYTHONUTF8'] = '1'
    $psi.Environment['PYTHONIOENCODING'] = 'utf-8'
    $process = [Diagnostics.Process]::Start($psi)
    $stdout = [IO.MemoryStream]::new()
    $stderr = [IO.MemoryStream]::new()
    $outTask = $process.StandardOutput.BaseStream.CopyToAsync($stdout)
    $errTask = $process.StandardError.BaseStream.CopyToAsync($stderr)
    $bytes = $utf8.GetBytes($Payload)
    $process.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
    $process.StandardInput.Close()
    $process.WaitForExit()
    [void]$outTask.GetAwaiter().GetResult()
    [void]$errTask.GetAwaiter().GetResult()
    $result = [pscustomobject]@{ exit_code = $process.ExitCode; stdout = $utf8.GetString($stdout.ToArray()); stderr = $utf8.GetString($stderr.ToArray()) }
    $stdout.Dispose(); $stderr.Dispose(); $process.Dispose()
    return $result
}
$version = Invoke-Tbrain -Arguments @('--version')
if ($version.exit_code -ne 0) { throw "--version failed: $($version.stderr)" }
$versionInfo = $version.stdout | ConvertFrom-Json
if ($Target -eq 'installed' -and $versionInfo.version -ne '0.7.3') { throw 'installed reproduction requires 0.7.3.' }
$probe = Invoke-Tbrain -Arguments @('--home', $AppHome, 'version')
if ($probe.exit_code -ne 0) { throw "version failed: $($probe.stderr)" }
$info = $probe.stdout | ConvertFrom-Json
if ([IO.Path]::GetFullPath($info.app_home) -ne $AppHome) { throw 'app_home isolation failed.' }
if ($Target -eq 'rc' -and [IO.Path]::GetFullPath($info.install_root) -ne $Root) { throw 'install_root isolation failed.' }
$forwarded = @('--home', $AppHome) + $CommandArgs
if ($Target -eq 'rc' -and $CommandArgs[0] -in @('install', 'upgrade', 'rollback', 'uninstall', 'doctor', 'connect', 'disconnect')) {
    $forwarded += @('--root', $Root)
}
$result = Invoke-Tbrain -Arguments $forwarded -Payload $InputJson
$record = @{ entry = $entry; version = $versionInfo; environment = $envPaths + @{ PYTHONUTF8 = '1'; PYTHONIOENCODING = 'utf-8' }; arguments = $forwarded; result = $result }
[IO.File]::AppendAllText($logPath, (($record | ConvertTo-Json -Compress -Depth 6) + "`n"), $utf8)
$result | Add-Member -NotePropertyName log_path -NotePropertyValue $logPath
$result | ConvertTo-Json -Depth 5
exit $result.exit_code
