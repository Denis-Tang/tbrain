#requires -Version 7.0
[CmdletBinding()]
param(
    [string]$Repo = (Split-Path $PSScriptRoot -Parent),
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'Programs/Tbrain'),
    [string]$SnapshotDir = (Join-Path (Get-Location).Path 'snapshots')
)

$ErrorActionPreference = 'Stop'
$userDir = [Environment]::GetFolderPath('UserProfile')
$appHome = Join-Path $userDir '.tbrain'
$lines = [Collections.Generic.List[string]]::new()
$differences = [Collections.Generic.List[string]]::new()
$utf8 = [Text.UTF8Encoding]::new($false)

function Git-Value([string[]]$GitArgs) {
    $value = & git -C $Repo @GitArgs 2>&1
    if ($LASTEXITCODE -ne 0) { throw "git $($GitArgs -join ' ') failed: $value" }
    return ($value -join "`n").Trim()
}

function Text-Hash([string]$Path) {
    $text = [IO.File]::ReadAllText($Path).Replace("`r`n", "`n")
    return [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($utf8.GetBytes($text))).ToLowerInvariant()
}

function File-Row([string]$Label, [string]$Path) {
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        $file = Get-Item -LiteralPath $Path
        $lines.Add("| $Label | $($file.Length) | $((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()) |")
    } else {
        $lines.Add("| $Label | 不存在 | — |")
    }
}

$lines.Add('# Tbrain 事实快照')
$lines.Add('')
$lines.Add("- 采样时间（UTC）：$([DateTimeOffset]::UtcNow.ToString('o'))")
$lines.Add("- 工作树：$Repo")
$lines.Add("- 当前提交：$(Git-Value @('rev-parse', '--short', 'HEAD'))")
$lines.Add('- 本脚本不调用 Tbrain，不读取凭据，不修改安装目录、宿主配置或运行进程。')
$lines.Add('')
$lines.Add('## Git 基座')
$lines.Add('')
$lines.Add('| 引用或命令 | 结果 |')
$lines.Add('|---|---|')
$expected = @{
    'v0.7.3^{commit}' = '2044893'; 'main' = '3952c80'
    'origin/main' = '2da1d97'; 'codex/fix-project-save' = '2e5f8ae'
}
foreach ($reference in @('v0.7.3^{commit}', 'v0.7.3', 'main', 'origin/main', 'codex/fix-project-save')) {
    $value = Git-Value @('rev-parse', '--short', $reference)
    $lines.Add("| $reference | $value |")
    if ($expected.ContainsKey($reference) -and $value -ne $expected[$reference]) {
        $differences.Add("附录 A2/A3：$reference = $value，原登记 $($expected[$reference])。")
    }
}
$lines.Add("| tag 类型 | $(Git-Value @('cat-file', '-t', 'v0.7.3')) |")
$lines.Add("| merge-base v0.7.3 / fix-project-save | $(Git-Value @('merge-base', 'v0.7.3', 'codex/fix-project-save')) |")
$lines.Add("| main...origin/main 左右提交数 | $(Git-Value @('rev-list', '--left-right', '--count', 'main...origin/main')) |")

$lines.Add('')
$lines.Add('## 已安装目录')
$lines.Add('')
$lines.Add('| 目录 | VERSION |')
$lines.Add('|---|---|')
$versions = @(Get-ChildItem -LiteralPath (Join-Path $InstallRoot 'versions') -Directory)
foreach ($directory in $versions) {
    $versionFile = Join-Path $directory.FullName 'VERSION'
    $version = if (Test-Path -LiteralPath $versionFile) { [IO.File]::ReadAllText($versionFile).Trim() } else { '不存在' }
    $lines.Add("| $($directory.Name) | $version |")
}
$currentFile = Join-Path $InstallRoot 'current.txt'
$currentVersion = [IO.File]::ReadAllText($currentFile).Trim()
$currentLink = Get-Item -LiteralPath (Join-Path $InstallRoot 'current')
$lines.Add('')
$lines.Add("- current.txt：$currentVersion")
$lines.Add("- current 链接目标：$($currentLink.Target -join ', ')")
$installedDir = Join-Path $InstallRoot "versions/$currentVersion"
foreach ($name in @('Tbrain.Desktop.exe', 'Tbrain.Desktop.dll')) {
    $desktopFile = Get-Item -LiteralPath (Join-Path $installedDir "desktop/$name")
    $lines.Add("- desktop/$name ProductVersion：$($desktopFile.VersionInfo.ProductVersion)")
}
if ($currentVersion -ne '0.7.3') { $differences.Add("附录 A1：current.txt = $currentVersion，原登记 0.7.3。") }
if ((($versions.Name | Sort-Object) -join ',') -ne '0.7.2,0.7.3,0.8.0-beta.1') {
    $differences.Add("附录 A1：版本目录为 $($versions.Name -join ', ')，与原登记三目录不同。")
}

$lines.Add('')
$lines.Add('## Tag 与安装文件哈希（CRLF 归一为 LF）')
$lines.Add('')
$lines.Add('| 文件 | tag SHA256 | 安装 SHA256 | 相同 |')
$lines.Add('|---|---|---|---|')
$archiveDir = Join-Path $env:TEMP ('tbrain-fact-' + [Guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $archiveDir
$archiveFile = Join-Path $archiveDir 'tag.zip'
$null = Git-Value @('archive', '--format=zip', "--output=$archiveFile", 'v0.7.3^{commit}', 'README.md', 'docs', 'integrations')
Expand-Archive -LiteralPath $archiveFile -DestinationPath (Join-Path $archiveDir 'tag')
$tagFiles = @(Git-Value @('ls-tree', '-r', '--name-only', 'v0.7.3^{commit}', '--', 'README.md', 'docs', 'integrations')) -split "`n"
$hashMatches = 0
foreach ($relative in $tagFiles) {
    $tagHash = Text-Hash (Join-Path $archiveDir "tag/$relative")
    $installedFile = Join-Path $installedDir $relative
    $installedHash = if (Test-Path -LiteralPath $installedFile -PathType Leaf) { Text-Hash $installedFile } else { '不存在' }
    $same = $tagHash -eq $installedHash
    if ($same) { $hashMatches++ } else { $differences.Add("附录 A4 扩展核对：$relative 不相同或安装文件不存在。") }
    $lines.Add("| $relative | $tagHash | $installedHash | $same |")
}
$lines.Add('')
$lines.Add("- 比对文件 $($tagFiles.Count) 个，相同 $hashMatches 个；这不能证明安装源码与 tag 相同。")
$lines.Add("- 临时 tag 解包目录：$archiveDir")

$lines.Add('')
$lines.Add('## 宿主 Tbrain 集成元数据')
$lines.Add('')
$lines.Add('| 文件 | 字节数 | SHA256 |')
$lines.Add('|---|---|---|')
File-Row 'pi 已部署扩展' (Join-Path $userDir '.pi/agent/extensions/tbrain.ts')
File-Row '随包 pi 扩展' (Join-Path $installedDir 'integrations/pi/tbrain.ts')
$lines.Add('')
$hooksFile = Join-Path $userDir '.codex/hooks.json'
if (Test-Path -LiteralPath $hooksFile) {
    $hooks = Get-Content -LiteralPath $hooksFile -Raw -Encoding utf8 | ConvertFrom-Json -AsHashtable
    foreach ($eventName in $hooks.hooks.Keys) {
        foreach ($group in $hooks.hooks[$eventName]) {
            foreach ($handler in $group.hooks) {
                foreach ($key in @('command', 'commandWindows')) {
                    if ($handler[$key] -match '(?i)tbrain') { $lines.Add("- Codex $eventName ${key}：$($handler[$key])") }
                }
            }
        }
    }
} else { $lines.Add('- Codex hooks.json：不存在。') }
$connectFile = Join-Path $appHome 'connect-state.json'
if (Test-Path -LiteralPath $connectFile) {
    $connectState = Get-Content -LiteralPath $connectFile -Raw -Encoding utf8 | ConvertFrom-Json -AsHashtable
    foreach ($hostName in ($connectState.Keys | Sort-Object)) {
        $lines.Add("- 宿主 $hostName enabled：$($connectState[$hostName].enabled)")
    }
} else { $lines.Add('- connect-state.json：不存在。') }

$lines.Add('')
$lines.Add('## 进程与登记')
$lines.Add('')
$lines.Add('- 全局 tbrain.exe 进程数：未测；原 §0.2 按映像名全局统计与 00 §5.7 冲突。')
$processDir = Join-Path $appHome 'processes'
$registrationCount = if (Test-Path -LiteralPath $processDir) { @(Get-ChildItem -LiteralPath $processDir -File -Filter '*.json').Count } else { 0 }
$lines.Add("- processes/*.json 文件数：$registrationCount（只列文件，不调用清理函数）。")
$wmiRoot = ($InstallRoot.TrimEnd('\', '/') + '\').Replace('\', '\\').Replace("'", "\'")
$scopedProcesses = @(Get-CimInstance Win32_Process -Filter "ExecutablePath LIKE '$wmiRoot%'" -Property Name, ProcessId, ParentProcessId)
$lines.Add("- 安装路径前缀内进程数：$($scopedProcesses.Count)；与全局数不等价。")
$lines.Add('')
$lines.Add('| 安装路径前缀内进程 | PID | 父进程名 | 父PID |')
$lines.Add('|---|---|---|---|')
foreach ($process in $scopedProcesses) {
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.ParentProcessId)" -Property Name, ProcessId
    $lines.Add("| $($process.Name) | $($process.ProcessId) | $($parent.Name) | $($process.ParentProcessId) |")
}

$lines.Add('')
$lines.Add('## Worktree 二维计数')
$lines.Add('')
$worktreeText = Git-Value @('worktree', 'list', '--porcelain')
$worktrees = @($worktreeText -split "`n`n" | ForEach-Object {
    $path = ($_ -split "`n" | Where-Object { $_.StartsWith('worktree ') }).Substring(9).Replace('\', '/')
    $prefix = if ($path.StartsWith(($userDir.Replace('\', '/') + '/.codex/worktrees/'), [StringComparison]::OrdinalIgnoreCase)) { 'codex' }
        elseif ($path -match '/\.tbrain-worktrees/') { 'tbrain' } else { '其他' }
    [pscustomobject]@{ Prefix = $prefix; Mode = if ($_ -match '(?m)^detached$') { 'detached' } else { 'branch' }; Id = Split-Path $path -Leaf }
})
$lines.Add('| 路径前缀 | detached/branch | 数量 |')
$lines.Add('|---|---|---|')
foreach ($group in ($worktrees | Group-Object Prefix, Mode | Sort-Object Name)) {
    $lines.Add("| $($group.Group[0].Prefix) | $($group.Group[0].Mode) | $($group.Count) |")
}
$detached = @($worktrees | Where-Object Mode -eq 'detached')
$lines.Add('')
$lines.Add("- worktree 总数：$($worktrees.Count)；detached：$($detached.Count)；branch：$($worktrees.Count - $detached.Count)。")
$lines.Add("- detached id：$($detached.Id -join ', ')")
if ($worktrees.Count -ne 39 -or $detached.Count -ne 5) { $differences.Add("附录 A10：worktree $($worktrees.Count) 条、detached $($detached.Count) 条，原登记 39 / 5。") }
$lines.Add('- 本快照不据名称推断 worktree 创建者或创建机制。')

$lines.Add('')
$lines.Add('## 更新状态')
$lines.Add('')
$updateFile = Join-Path $appHome 'update-state.json'
if (Test-Path -LiteralPath $updateFile) {
    $updateState = Get-Content -LiteralPath $updateFile -Raw -Encoding utf8 | ConvertFrom-Json -AsHashtable
    $lines.Add("- stage：$($updateState.stage)")
    $lines.Add("- message：$($updateState.message)")
    if ($updateState.stage -ne 'replacing' -or $updateState.message -ne 'unknown encoding: utf-16-le') { $differences.Add('附录 A6：更新状态与原登记不同，见本节实际字段。') }
} else { $lines.Add('- update-state.json：不存在。') }

$lines.Add('')
$lines.Add('## 与附录不一致')
$lines.Add('')
if ($differences.Count) { foreach ($difference in $differences) { $lines.Add('- ' + $difference) } }
else { $lines.Add('- 已比较项目未发现差异；全局进程数未测。') }
$lines.Add('')
$lines.Add('- 未读取 auth.json、models.json、环境凭据或凭据存储；未调用 Tbrain。')

$stamp = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTimeOffset]::UtcNow, 'China Standard Time').ToString('yyyy-MM-dd-HHmmss')
New-Item -ItemType Directory -Path $SnapshotDir -Force | Out-Null
$output = Join-Path $SnapshotDir "事实快照-$stamp.md"
$stream = [IO.File]::Open($output, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
$writer = [IO.StreamWriter]::new($stream, $utf8)
try { $writer.Write(($lines -join "`n") + "`n") } finally { $writer.Dispose() }
Write-Output $output
