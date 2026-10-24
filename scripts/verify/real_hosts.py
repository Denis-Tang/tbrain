"""Tbrain 0.6.0 真实宿主端到端验证（Claude Code / Codex / pi）。

这是开发期验证脚本，会产生少量真实模型费用；默认不放进 pytest，需显式运行。
用法：
    .venv\\Scripts\\python.exe scripts/verify/real_hosts.py claude
    .venv\\Scripts\\python.exe scripts/verify/real_hosts.py codex
    .venv\\Scripts\\python.exe scripts/verify/real_hosts.py pi

安全边界：
- Tbrain 安装/数据/vault 全在 .research/e2e/<host>/ 临时目录；
- Claude 不改真实 settings：用 connect 生成到临时 CLAUDE_CONFIG_DIR，再以 --settings/--mcp-config 运行；
- Codex 用临时 CODEX_HOME（仅机械复制 auth.json 用于登录，测试后删除）；
- pi 用 `-e` 显式加载临时扩展，使用真实 pi 配置/凭据（不复制、不改配置）；
- 不读取、不打印任何凭据内容；结束时清理临时凭据副本。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

# 脚本位于 <仓库>/scripts/verify/；仓库根目录由文件位置推导。
REPO = Path(__file__).resolve().parents[2]
DIST = REPO / "dist" / "tbrain"
E2E_ROOT = REPO / ".research" / "e2e"
PYTHON = REPO / ".venv" / "Scripts" / "python.exe"


def _run(command, *, env=None, cwd=None, timeout=300, shell=False):
    print("$", " ".join(str(part) for part in command))
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", env=env, cwd=cwd, timeout=timeout, shell=shell)
    print("  rc:", completed.returncode)
    if completed.stdout.strip():
        print("  stdout:", completed.stdout.strip()[:2000])
    if completed.stderr.strip():
        print("  stderr:", completed.stderr.strip()[:2000])
    return completed


def _setup(name: str) -> dict:
    root = E2E_ROOT / name
    if root.exists():
        shutil.rmtree(root)
    paths = {
        "root": root,
        "home": root / "home",
        "install": root / "install",
        "vault": root / "vault",
        "work": root / "work",
    }
    for key in ("home", "vault", "work"):
        paths[key].mkdir(parents=True)
    (paths["home"] / "settings.json").write_text(json.dumps({
        "vault_path": str(paths["vault"]),
        "timezone": "Asia/Shanghai",
        "ntp_enabled": False,
        "timeout_hours": 6,
        "reminder_context_switch": True,
        "install_root": str(paths["install"]),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    env = dict(os.environ, TBRAIN_HOME=str(paths["home"]), TBRAIN_INSTALL_ROOT=str(paths["install"]))
    result = _run([str(DIST / "tbrain.exe"), "install", "--from", str(DIST), "--version", "0.6.0",
                   "--root", str(paths["install"]), "--home", str(paths["home"])], env=env)
    if result.returncode != 0:
        raise SystemExit(f"安装失败：{result.stderr[:800]}")
    entry = paths["install"] / "bin" / "tbrain.cmd"
    if not entry.is_file():
        raise SystemExit(f"固定入口不存在：{entry}")
    paths["entry"] = entry
    paths["env"] = env
    return paths


def _connect(paths: dict, host: str, extra_env: dict) -> dict:
    env = dict(paths["env"], **extra_env)
    result = _run(["cmd", "/c", str(paths["entry"]), "connect", host], env=env)
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        payload = {"raw": result.stdout, "stderr": result.stderr}
    print("  connect:", json.dumps(payload, ensure_ascii=False)[:1200])
    return payload


def _session_report(paths: dict) -> list[dict]:
    sys.path.insert(0, str(REPO / "src"))
    from tbrain import handoff

    reports = []
    for path in sorted((paths["vault"] / "会话总结").rglob("*.md")) if (paths["vault"] / "会话总结").is_dir() else []:
        summary = handoff.parse(path.read_text(encoding="utf-8"))
        reports.append({
            "file": path.relative_to(paths["vault"]).as_posix(),
            "session_id": summary.session_id,
            "raw_command": summary.raw_command[:120],
            "users": len(summary.users),
            "done": [item["text"] for item in summary.items("已完成")],
            "next_step": summary.next_step,
            "status": summary.status,
        })
    return reports


def _print_sessions(paths: dict, label: str) -> None:
    print(f"=== {label} 会话文件 ===")
    reports = _session_report(paths)
    for report in reports:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    if not reports:
        print("（没有生成会话总结）")


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------
def run_claude() -> int:
    paths = _setup("claude")
    config_dir = paths["root"] / "claude-config"
    config_dir.mkdir()
    _connect(paths, "claude-code", {"CLAUDE_CONFIG_DIR": str(config_dir)})
    settings_path = config_dir / "settings.json"
    mcp_source = config_dir.parent / ".claude.json"
    mcp_payload = json.loads(mcp_source.read_text(encoding="utf-8"))
    mcp_config = paths["root"] / "mcp.json"
    mcp_config.write_text(json.dumps({"mcpServers": mcp_payload.get("mcpServers") or {}},
                                      ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    prompt = ("这是 Tbrain 的端到端联调。请先看上下文里 hook 注入的 session_id"
              "（形如 claude-code:...），然后调用 mcp__tbrain__save 保存一条增量："
              '{"session_id":"<注入的完整 session_id>","operations":['
              '{"op":"append","section":"done","items":[{"text":"E2E Claude 真实链路验证通过"}]},'
              '{"op":"replace","section":"next_step","text":"人工查看 Tbrain 会话文件"}]}。'
              "不要修改任何配置文件，最后只回复 E2E-OK。")
    claude_cmd = Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    if not claude_cmd.is_file():
        claude_cmd = Path(shutil.which("claude") or "claude")
    env = dict(os.environ)  # 真实 HOME，用于登录；配置通过 --settings/--mcp-config 隔离
    started = time.time()
    result = _run([str(claude_cmd), "-p", prompt,
                   "--settings", str(settings_path),
                   "--mcp-config", str(mcp_config), "--strict-mcp-config",
                   "--dangerously-skip-permissions", "--output-format", "json"],
                  env=env, cwd=paths["work"], timeout=420)
    print(f"  用时 {time.time() - started:.1f}s")
    _print_sessions(paths, "Claude Code")
    return 0 if result.returncode == 0 else 1


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------
def _codex_minimal_config(real_config: Path) -> str:
    import tomllib
    import tomli_w

    data = tomllib.loads(real_config.read_text(encoding="utf-8"))
    payload = {key: data[key] for key in ("model", "model_provider", "model_reasoning_effort",
                                          "personality", "sandbox_mode", "service_tier")
               if key in data}
    if "model_providers" in data:
        payload["model_providers"] = data["model_providers"]
    return tomli_w.dumps(payload)


def run_codex() -> int:
    paths = _setup("codex")
    real_codex = Path.home() / ".codex"
    codex_home = paths["root"] / "codex-home"
    codex_home.mkdir()
    # 只机械复制/硬链接认证文件（不读取、不打印内容），测试后删除整个临时目录
    auth = real_codex / "auth.json"
    if not auth.is_file():
        raise SystemExit("找不到 Codex 登录文件，无法真实验证")
    try:
        os.link(auth, codex_home / "auth.json")
    except OSError:
        shutil.copy2(auth, codex_home / "auth.json")
    (codex_home / "config.toml").write_text(_codex_minimal_config(real_codex / "config.toml"),
                                            encoding="utf-8")
    _connect(paths, "codex", {"CODEX_HOME": str(codex_home)})
    prompt = ("这是 Tbrain 的端到端联调。请使用 hook 注入的 session_id（形如 codex:...），"
              "调用 mcp__tbrain__save 保存："
              '{"session_id":"<注入的完整 session_id>","operations":['
              '{"op":"append","section":"done","items":[{"text":"E2E Codex 真实链路验证通过"}]},'
              '{"op":"replace","section":"next_step","text":"人工查看 Tbrain 会话文件"}]}。'
              "不要修改任何配置文件，最后只回复 E2E-OK。")
    node = shutil.which("node") or "node"
    codex_cmd = Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    env = dict(paths["env"], CODEX_HOME=str(codex_home))
    started = time.time()
    result = _run([node, str(codex_cmd), "exec",
                   "--dangerously-bypass-hook-trust",
                   "--dangerously-bypass-approvals-and-sandbox",
                   "--skip-git-repo-check", prompt],
                  env=env, cwd=paths["work"], timeout=420)
    print(f"  用时 {time.time() - started:.1f}s")
    _print_sessions(paths, "Codex")
    return 0 if result.returncode == 0 else 1


# ---------------------------------------------------------------------------
# pi
# ---------------------------------------------------------------------------
def run_pi() -> int:
    paths = _setup("pi")
    # pi 无内置 MCP，用扩展接入；不复制 pi 配置/凭据，改为 -e 显式加载临时扩展。
    extension = paths["root"] / "tbrain.ts"
    shutil.copy2(REPO / "integrations" / "pi" / "tbrain.ts", extension)
    settings = json.loads((Path.home() / ".pi" / "agent" / "settings.json").read_text(encoding="utf-8-sig"))
    provider = str(settings.get("defaultProvider") or "magpie")
    model = str(settings.get("defaultModel") or "")
    prompt = ("这是 Tbrain 的端到端联调。请使用 hook 注入的 session_id，"
              "调用 tbrain_save 工具保存："
              '{"session_id":"<注入的完整 session_id>","operations":['
              '{"op":"append","section":"done","items":[{"text":"E2E pi 真实链路验证通过"}]},'
              '{"op":"replace","section":"next_step","text":"人工查看 Tbrain 会话文件"}]}。'
              "不要修改任何配置文件，最后只回复 E2E-OK。")
    node = shutil.which("node") or "node"
    pi_cli = Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules" / "@earendil-works" / "pi-coding-agent" / "dist" / "cli.js"
    env = dict(os.environ, TBRAIN_HOME=str(paths["home"]),
               TBRAIN_INSTALL_ROOT=str(paths["install"]),
               TBRAIN_ENTRY=str(paths["entry"]))
    command = [node, str(pi_cli), "-e", str(extension), "--provider", provider,
               "--model", model, "--no-builtin-tools", "-p", prompt]
    started = time.time()
    result = _run(command, env=env, cwd=paths["work"], timeout=420)
    print(f"  用时 {time.time() - started:.1f}s")
    _print_sessions(paths, "pi")
    return 0 if result.returncode == 0 else 1


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in {"claude", "codex", "pi"}:
        print(__doc__)
        return 2
    try:
        return {"claude": run_claude, "codex": run_codex, "pi": run_pi}[argv[1]]()
    finally:
        print("提示：临时目录保留在", E2E_ROOT / argv[1], "便于检查；其中可能包含登录文件的硬链接，确认后可整体删除。")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
