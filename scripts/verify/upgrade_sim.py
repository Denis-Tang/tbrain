"""第 3 步验收：在临时目录里模拟安装 0.6.0 → 升级到 0.6.1，验证进程、入口、配置不变。

用真实构建的两个分发包（.research/packages/tbrain-0.6.0 / tbrain-0.6.1，
可先按 scripts/verify/README.md 生成），全程在 .research/upgrade-sim 临时目录，
不碰真实安装和宿主配置。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

# 脚本位于 <仓库>/scripts/verify/；仓库根目录由文件位置推导。
REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / ".research" / "upgrade-sim"
PACKAGES = REPO / ".research" / "packages"
PY = REPO / ".venv" / "Scripts" / "python.exe"


def run(command, env, timeout=300):
    print("$", " ".join(str(part) for part in command))
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", env=env, timeout=timeout)
    print("  rc:", completed.returncode)
    if completed.stdout.strip():
        print("  out:", completed.stdout.strip()[:1500])
    if completed.stderr.strip():
        print("  err:", completed.stderr.strip()[:1000])
    return completed


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    if ROOT.exists():
        import shutil

        shutil.rmtree(ROOT)
    home = ROOT / "home"
    install = ROOT / "install"
    vault = ROOT / "vault"
    work = ROOT / "work"
    for folder in (home, vault, work):
        folder.mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({
        "vault_path": str(vault), "timezone": "Asia/Shanghai", "ntp_enabled": False,
        "timeout_hours": 6, "reminder_context_switch": True, "install_root": str(install),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    env = dict(os.environ, TBRAIN_HOME=str(home), TBRAIN_INSTALL_ROOT=str(install),
               HOME=str(ROOT / "fake-home"), USERPROFILE=str(ROOT / "fake-home"))
    (ROOT / "fake-home").mkdir(parents=True)

    # 1) 安装 0.6.0
    result = run([str(PACKAGES / "tbrain-0.6.0" / "tbrain.exe"), "install",
                  "--from", str(PACKAGES / "tbrain-0.6.0"), "--version", "0.6.0",
                  "--root", str(install), "--home", str(home)], env)
    if result.returncode != 0:
        return 1
    entry = install / "bin" / "tbrain.cmd"
    assert entry.is_file(), "固定入口不存在"
    assert (install / "current.txt").read_text(encoding="utf-8").strip() == "0.6.0"

    # 2) 用固定入口给四个宿主写配置（伪造 HOME/DSH）
    dsh_home = ROOT / "dsh"
    (dsh_home / "profiles" / "desktop").mkdir(parents=True)
    (dsh_home / "profiles" / "desktop" / "cordis.patch.yml").write_text("[]\n", encoding="utf-8")
    (dsh_home / "profiles" / "desktop" / "package.json").write_text('{"name":"p"}\n', encoding="utf-8")
    host_env = dict(env, DSH_HOME=str(dsh_home),
                    CLAUDE_CONFIG_DIR=str(ROOT / "claude-config"))
    (ROOT / "claude-config").mkdir()
    for host in ("claude-code", "codex", "dsh", "pi"):
        result = run(["cmd", "/c", str(entry), "connect", host,
                      "--home", str(home), "--root", str(install)], host_env)
        if result.returncode != 0:
            print(f"connect {host} 失败")
            return 1
    config_paths = sorted([path for path in ROOT.rglob("*") if path.is_file()
                           and ("claude" in path.as_posix() or ".codex" in path.as_posix()
                                or "dsh" in path.as_posix() or ".pi" in path.as_posix()
                                or path.name in {"hooks.json"})])
    before = {str(path.relative_to(ROOT)): sha(path) for path in config_paths}
    print("宿主配置哈希数量:", len(before))

    # 3) 启动 0.6.0 的 MCP 长驻进程
    mcp = subprocess.Popen(["cmd", "/c", str(entry), "--home", str(home), "mcp"],
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, env=env)
    registry = home / "processes"
    deadline = time.time() + 30
    registered = []
    while time.time() < deadline:
        registered = list(registry.glob("*.json")) if registry.is_dir() else []
        if any(json.loads(path.read_text(encoding="utf-8-sig")).get("role") == "mcp"
               for path in registered):
            break
        time.sleep(0.3)
    mcp_record = [json.loads(path.read_text(encoding="utf-8-sig")) for path in registered]
    mcp_pid = next((item["pid"] for item in mcp_record if item.get("role") == "mcp"), None)
    print("0.6.0 MCP 进程:", mcp_pid, "版本:", [item.get("version") for item in mcp_record])
    if mcp_pid is None:
        return 1

    # 4) 从 0.6.0 安装入口升级到 0.6.1（upgrade 从本地包目录）
    result = run(["cmd", "/c", str(entry), "upgrade",
                  "--from", str(PACKAGES / "tbrain-0.6.1"), "--version", "0.6.1",
                  "--root", str(install), "--home", str(home)], env, timeout=600)
    if result.returncode != 0:
        return 1

    # 5) 验证
    current = (install / "current.txt").read_text(encoding="utf-8").strip()
    versions = sorted(path.name for path in (install / "versions").iterdir() if path.is_dir())
    print("current:", current, "versions:", versions)
    assert current == "0.6.1", current
    assert "0.6.0" in versions and "0.6.1" in versions
    assert len(versions) <= 2, versions
    time.sleep(1.5)
    alive = _pid_alive(mcp_pid)
    print("旧 MCP 进程存活:", alive)
    assert not alive, "旧版本进程仍然存活"
    after = {str(path.relative_to(ROOT)): sha(path) for path in config_paths}
    changed = [name for name in before if before.get(name) != after.get(name)]
    print("宿主配置变化:", changed)
    assert not changed, f"宿主配置被改动：{changed}"

    # 6) 新入口 doctor + session 基础工作
    result = run(["cmd", "/c", str(entry), "doctor", "--home", str(home), "--root", str(install)], host_env)
    report = json.loads(result.stdout)
    if not isinstance(report, dict) or "items" not in report:
        print("doctor 输出无效")
        return 1
    print("doctor:", [(item["id"], item["status"]) for item in report["items"]])
    assert all(item["status"] != "red" for item in report["items"]), report
    ok = run([str(install / "current" / "tbrain.exe"), "--version"], env)
    assert "0.6.1" in ok.stdout, ok.stdout
    mcp.terminate()
    print("升级模拟通过")
    return 0


def _pid_alive(pid: int) -> bool:
    try:
        completed = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                   capture_output=True, text=True, timeout=20)
        return str(pid) in completed.stdout
    except (OSError, subprocess.SubprocessError):
        return False


if __name__ == "__main__":
    raise SystemExit(main())
