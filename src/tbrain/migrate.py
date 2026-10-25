"""旧 Shared Brain 数据迁移（第 6 步 6.2）。

只读旧应用数据目录与旧 vault；把可映射的会话总结、项目卡、公共池对象、全局提示词
写入新 vault。旧数据一律不改动，迁移幂等（按来源路径 + 内容哈希去重）。
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from typing import Any

from . import handoff, naming
from . import store as store_module

HOST_MAP = {
    "claude": "claude-code", "claude code": "claude-code", "claudecode": "claude-code",
    "codex": "codex", "codex cli": "codex", "codex desktop": "codex",
    "deepseek-harness": "dsh", "deepseek harness": "dsh", "oh-dsh": "dsh", "dsh": "dsh",
    "pi": "pi", "pi coding agent": "pi",
}
PUBLIC_KINDS = store_module.PUBLIC_KINDS
_OBJECT_RULES = (
    ("PATH", ("path", "环境变量 path")),
    ("代理", ("代理", "proxy", "proxychains")),
    ("工作区", ("工作区", "workspace", "工作目录")),
    ("安装", ("安装", "install", "winget", "scoop", "setup", "程序")),
    ("环境变量", ("环境变量", "env ", "环境 变量")),
    ("配置", ("配置", "config", "settings", "agents.md", "claude.md", "toml", "yaml", "yml", "json", ".cmd")),
)


# ---------------------------------------------------------------------------
# 只读读取旧数据
# ---------------------------------------------------------------------------

def _sha(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _utc_iso(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _old_host(agent: str) -> str:
    return HOST_MAP.get(str(agent or "").strip().lower(), "unknown")


def _norm_host(agent: str) -> str:
    return _old_host(agent)


def _read_front(text: str) -> dict:
    if not text.startswith("---", 0):
        return {}
    end = text.find("\n---", 4)
    if end == -1:
        return {}
    try:
        import yaml

        data = yaml.safe_load(text[4:end]) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _body(text: str) -> str:
    if not text.startswith("---", 0):
        return text.strip("\n")
    end = text.find("\n---", 4)
    return text[end + 4:].strip("\n") if end != -1 else text


def _load_project_paths(con) -> dict[str, list[str]]:
    try:
        row = con.execute("SELECT value FROM state WHERE key='project_paths'").fetchone()
        data = json.loads(row[0]) if row else {}
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


def read_old(old_home: Path) -> dict:
    old = Path(old_home).expanduser()
    result: dict[str, Any] = {
        "old_home": old, "exists": old.is_dir(), "vault": None, "settings": {},
        "projects": [], "sessions": [], "objects": [], "global_prompt": "",
        "errors": [], "skip_files": [],
    }
    if not old.is_dir():
        result["errors"].append("旧数据目录不存在")
        return result
    settings_path = old / "settings.json"
    if settings_path.is_file():
        try:
            result["settings"] = json.loads(settings_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            result["errors"].append(f"settings.json 读取失败：{exc}")
    vault_text = str(result["settings"].get("vault_path") or "")
    if vault_text:
        vault = Path(vault_text)
        result["vault"] = vault
    con = None
    db = next(iter(sorted(old.glob("index-*.sqlite3"))), None)
    if db is not None:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
        except Exception as exc:
            result["errors"].append(f"旧索引打开失败：{exc}")
            con = None
    project_paths = _load_project_paths(con) if con is not None else {}
    vault = result["vault"]
    if vault and vault.is_dir():
        # 项目卡（含立项归档，归档项目只作为映射来源，不导入）
        for card in sorted((vault / "项目").glob("*/*.md")):
            if card.name not in ("项目.md", "立项归档.md"):
                continue
            text = card.read_text(encoding="utf-8")
            meta = _read_front(text)
            project_id = str(meta.get("project_id") or meta.get("project") or "").strip()
            if not project_id:
                continue
            result["projects"].append({
                "project_id": project_id,
                "name": str(meta.get("title") or card.parent.name).strip(),
                "paths": project_paths.get(project_id) or [],
                "parent_id": str(meta.get("parent_project_id") or "").strip(),
                "created": _utc_iso(str(meta.get("created") or "")),
                "archived": card.name == "立项归档.md",
                "source": card.relative_to(vault).as_posix(),
            })
        # 会话总结
        for path in sorted((vault / "会话总结").rglob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                result["skip_files"].append(f"{path}: {exc}")
                continue
            meta = _read_front(text)
            result["sessions"].append({
                "source": path.relative_to(vault).as_posix(),
                "meta": meta,
                "project_id": str(meta.get("project") or "").strip(),
                "agent": str(meta.get("agent") or ""),
                "session_id": str(meta.get("session_id") or "").strip(),
                "title": str(meta.get("title") or "").strip(),
                "created": _utc_iso(str(meta.get("created") or "")),
                "updated": _utc_iso(str(meta.get("updated") or meta.get("created") or "")),
                "body": _body(text),
                "hash": _sha(text),
            })
        prompt_path = vault / "知识" / "全局提示词.md"
        if prompt_path.is_file():
            result["global_prompt"] = prompt_path.read_text(encoding="utf-8")
    if con is not None:
        try:
            for row in con.execute("SELECT path,title,body,metadata FROM notes WHERE kind='object' ORDER BY path"):
                meta = {}
                try:
                    meta = json.loads(row["metadata"] or "{}")
                except ValueError:
                    meta = {}
                result["objects"].append({
                    "source": str(row["path"]),
                    "title": str(meta.get("title") or row["title"] or "").strip(),
                    "body": str(row["body"] or ""),
                    "metadata": meta,
                    "updated": _utc_iso(str(meta.get("updated") or meta.get("created") or "")),
                    "hash": _sha(json.dumps([row["path"], row["body"], row["metadata"]],
                                            ensure_ascii=False, sort_keys=True)),
                })
        except Exception as exc:
            result["errors"].append(f"读取旧对象失败：{exc}")
        finally:
            con.close()
    return result


def _project_index(old: dict) -> tuple[dict[str, dict], dict[str, str]]:
    by_id: dict[str, dict] = {}
    by_name: dict[str, str] = {}
    for project in old["projects"]:
        by_id[project["project_id"]] = project
        by_name[project["name"]] = project["project_id"]
    return by_id, by_name


def _project_name(old: dict, project_id: str) -> str:
    if not project_id:
        return ""
    by_id, _ = _project_index(old)
    project = by_id.get(project_id)
    if not project or project.get("archived"):
        return ""
    return project["name"]


def _public_kind(title: str, location: str) -> str:
    haystack = f"{title} {location}".lower()
    for kind, terms in _OBJECT_RULES:
        if any(term in haystack for term in terms):
            return kind
    return "工具"


# ---------------------------------------------------------------------------
# 计划（dry-run）
# ---------------------------------------------------------------------------

def plan(old_home: Path, new_vault: Path) -> dict:
    old = read_old(old_home)
    by_id, _ = _project_index(old)
    kinds: dict[str, int] = {}
    for object_row in old["objects"]:
        kind = _public_kind(object_row["title"], str(object_row["metadata"].get("location") or ""))
        kinds[kind] = kinds.get(kind, 0) + 1
    active_projects = [item for item in old["projects"] if not item.get("archived")]
    archived_projects = [item for item in old["projects"] if item.get("archived")]
    report = {
        "mode": "dry-run",
        "old_home": str(old["old_home"]),
        "old_home_exists": old["exists"],
        "old_vault": str(old["vault"] or ""),
        "old_vault_exists": bool(old["vault"] and old["vault"].is_dir()),
        "new_vault": str(new_vault),
        "writes": "无（dry-run 只读）",
        "errors": old["errors"],
        "skip_files": old["skip_files"],
        "counts": {
            "sessions": len(old["sessions"]),
            "projects": len(active_projects),
            "archived_projects": len(archived_projects),
            "objects": len(old["objects"]),
            "global_prompt": bool(old["global_prompt"]),
        },
        "public_object_kinds": kinds,
        "import_plan": [
            {"source": "旧 vault 会话总结（会话总结/**/*.md）", "count": len(old["sessions"]),
             "target": "会话总结/<项目或独立>/YYYY/MM/<名称>.md",
             "action": "正文保留（放入“旧总结原文（迁移）”节）；YAML 头按 handoff/v1 重建；旧标题作为目标；状态=已完成"},
            {"source": "旧 vault 项目卡（项目/*/项目.md）", "count": len(active_projects),
             "target": "项目/<名>/项目卡.md + 会话索引.md",
             "action": "名称、工作目录、父项目（按 project_id 映射）、创建时间迁移"},
            {"source": "旧索引 notes(kind=object)", "count": len(old["objects"]),
             "target": "公共池/<类型>/<名称>.md",
             "action": "按对象标题/位置推断类型；位置、状态、验证、历史与来源保留"},
        ],
        "unmapped": [
            {"source": "旧知识 memory", "reason": "维护轮产物，第一期不导入"},
            {"source": "旧技能 skill", "reason": "维护轮产物，第一期不导入"},
            {"source": "旧决策 decision / 旧任务 task", "reason": "模型不同，需人工整理"},
            {"source": "旧索引/FTS/维护进度/错误 JSON/费用账本/附件注册", "reason": "程序重建或第一期不做"},
            {"source": "已归档项目卡（立项归档.md）", "count": len(archived_projects),
             "reason": "只作为旧会话的项目名映射来源，不导入为活跃项目"},
        ],
        "warnings": [
            "旧会话没有逐字“原始命令”和 U# 记录：原始命令置空，全文保存在“旧总结原文（迁移）”节",
            "旧项目提示词没有 handoff 结构：不自动生成新项目提示词，保留旧 vault 供人工参考",
        ],
    }
    return report


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------

def _empty_new_vault(vault: Path) -> bool:
    if not vault.exists():
        return True
    return not any(path.is_file() for path in vault.rglob("*"))


def _backup_new_vault(vault: Path) -> str:
    if _empty_new_vault(vault):
        return ""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = vault.with_name(f"{vault.name}.migration-backup-{stamp}")
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(vault, target)
    return str(target)


def _load_ledger(home: Path) -> dict:
    path = Path(home) / "migration-ledger.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def _save_ledger(home: Path, ledger: dict) -> None:
    path = Path(home) / "migration-ledger.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"migration-ledger.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def execute(service, old_home: Path, new_vault: Path | None = None) -> dict:
    """执行迁移；返回报告。旧数据只读，新 vault 写入失败会逐项记录并继续。"""
    old = read_old(old_home)
    vault = Path(new_vault or service.vault_root()).expanduser()
    report = {
        "mode": "execute",
        "old_home": str(old["old_home"]),
        "old_vault": str(old["vault"] or ""),
        "new_vault": str(vault),
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "imported": {"sessions": 0, "projects": 0, "objects": 0, "global_prompt": 0},
        "updated": {"objects": 0},
        "skipped": {"sessions": 0, "projects": 0, "objects": 0, "global_prompt": 0},
        "failed": [],
        "warnings": list(plan(old["old_home"], vault)["warnings"]),
        "backup": "",
        "ledger": str(Path(service.home) / "migration-ledger.json"),
        "report_path": "",
        "errors": old["errors"],
    }
    if not old["exists"] or not (old["vault"] and old["vault"].is_dir()):
        report["failed"].append("旧数据目录或旧 vault 不存在，已停止")
        return report
    if vault.resolve() == old["vault"].resolve():
        report["failed"].append("新 vault 不能与旧 vault 相同")
        return report
    if str(vault.resolve()).lower().startswith(str(old["vault"].resolve()).lower() + os.sep):
        report["failed"].append("新 vault 不能位于旧 vault 内部")
        return report
    vault.mkdir(parents=True, exist_ok=True)
    report["backup"] = _backup_new_vault(vault)
    service._vault_override = vault  # 迁移直接写目标 vault
    service.reset_vault()
    service.store.ensure_layout()
    ledger = _load_ledger(service.home)
    by_id, _ = _project_index(old)

    # 1) 项目卡
    for project in old["projects"]:
        if project.get("archived"):
            continue
        source = f"project:{project['project_id']}"
        if source in ledger and (service.store.project(project["name"]) or {}):
            report["skipped"]["projects"] += 1
            continue
        if service.store.project(project["name"]):
            report["skipped"]["projects"] += 1
            ledger[source] = {"target": str(service.store.project_card_path(project["name"])), "hash": project["project_id"]}
            continue
        try:
            card = service.store.project_card_path(project["name"])
            data = {
                "name": project["name"],
                "paths": [str(path) for path in project["paths"]],
                "parent": _project_name(old, project["parent_id"]),
                "allow_child": False,
                "created_at": project["created"] or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            service.store.write_text(card, store_module.render_note(data, "迁移自旧 Shared Brain 项目卡。"))
            service.store._ensure_project_files(project["name"])
            ledger[source] = {"target": str(card), "hash": project["project_id"]}
            report["imported"]["projects"] += 1
        except Exception as exc:
            report["failed"].append({"kind": "project", "source": project["source"], "error": str(exc)})

    # 2) 会话总结
    existing_names = {item.get("name") for item in service.index.sessions()}
    sequence_by_key: dict[tuple[str, str], int] = {}
    for session in old["sessions"]:
        if session["hash"] in ledger.get("session_hashes", {}).values():
            report["skipped"]["sessions"] += 1
            continue
        project_name = _project_name(old, session["project_id"])
        try:
            host = _old_host(session["agent"])
            created = datetime.fromisoformat(session["created"].replace("Z", "+00:00")) if session["created"] else datetime.now(timezone.utc)
            date_text = created.strftime("%Y-%m-%d")
            key = (project_name, date_text)
            sequence = sequence_by_key.get(key, 0) + 1
            sequence_by_key[key] = sequence
            title = session["title"].strip()
            if title in ("", "会话总结", "会话记录", "总结"):
                first_line = next((part.strip() for part in session["body"].splitlines() if part.strip()), "")
                title = first_line[:120] or "旧会话"
            name = naming.session_name(title, project_name, list(existing_names), date_text, sequence)
            summary = handoff.new_summary(
                name=name, project=project_name, session_id=f"{host}:{session['session_id'] or 'migrated-' + session['hash'][:12]}",
                agent=host, cwd="", host_record="", started_at=session["created"] or session["updated"],
                raw_command="", status="已完成", provisional=False,
            )
            summary.header["updated_at"] = session["updated"] or summary.header["started_at"]
            summary.header["last_activity_at"] = summary.header["updated_at"]
            summary.target = title[:handoff.TARGET_MAX_CHARS]
            summary.sections["用户指令"] = []
            summary.header["saved_users"] = 0
            summary.extra["旧总结原文（迁移）"] = (
                f"旧文件：{session['source']}\n\n{session['body']}".strip()
            )
            path = service.store.session_path(project_name or "独立", created, name)
            service.store.write_text(path, handoff.render(summary))
            relative = path.resolve().relative_to(vault.resolve()).as_posix()
            service.index.upsert_session(service._row_from_summary(summary, relative), handoff.render(summary))
            ledger.setdefault("session_hashes", {})[session["source"]] = session["hash"]
            existing_names.add(name)
            report["imported"]["sessions"] += 1
        except Exception as exc:
            report["failed"].append({"kind": "session", "source": session["source"], "error": str(exc)})

    # 3) 公共池对象
    for object_row in old["objects"]:
        if object_row["hash"] in ledger.get("object_hashes", {}).values():
            report["skipped"]["objects"] += 1
            continue
        meta = object_row["metadata"]
        title = object_row["title"] or Path(object_row["source"]).stem
        kind = _public_kind(title, str(meta.get("location") or ""))
        note = {
            "version": str(meta.get("version") or ""),
            "install": str(meta.get("install") or ""),
            "package_id": str(meta.get("package_id") or ""),
            "location": str(meta.get("location") or ""),
            "scope": str(meta.get("scope") or ""),
            "uninstall": str(meta.get("uninstall") or ""),
            "verify": str(meta.get("evidence") or ""),
            "state": "生效" if meta.get("verified") else "生效（未验证）",
            "note": "迁移自旧 Shared Brain 对象",
            "created_at": _utc_to_local_iso(str(meta.get("created") or "")),
            "body": (
                (str(meta.get("current_state") or "") + "\n\n" if meta.get("current_state") else "")
                + (object_row["body"] or "")
            ).strip(),
        }
        try:
            result = service.store.save_public_card(
                kind, title, note,
                source_session=str(meta.get("session_id") or ""),
            )
            ledger.setdefault("object_hashes", {})[object_row["source"]] = object_row["hash"]
            if result.get("replaced"):
                report["updated"]["objects"] += 1
            else:
                report["imported"]["objects"] += 1
        except Exception as exc:
            report["failed"].append({"kind": "object", "source": object_row["source"], "error": str(exc)})

    # 4) 全局提示词
    if old["global_prompt"]:
        current = service.store.global_prompt()
        has_user_text = "用户补充" not in current or len(current.strip()) > 0
        user_block = ""
        if "<!-- owner:user -->" in current:
            start = current.find("<!-- owner:user -->") + len("<!-- owner:user -->")
            end = current.find("<!-- /owner:user -->", start)
            user_block = current[start:end].strip() if end != -1 else current.strip()
        if user_block.strip() and user_block.strip() != "## 用户补充":
            report["skipped"]["global_prompt"] += 1
            report["warnings"].append("新 vault 全局提示词用户区已有内容，旧全局提示词未覆盖；旧内容保留在旧 vault")
        else:
            try:
                service.store.save_global_prompt(old["global_prompt"])
                report["imported"]["global_prompt"] = 1
            except Exception as exc:
                report["failed"].append({"kind": "global_prompt", "source": "知识/全局提示词.md", "error": str(exc)})

    service.refresh_index()
    _save_ledger(service.home, ledger)
    report["finished_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report["report_path"] = str(_write_report(vault, report))
    return report


def _utc_to_local_iso(value: str) -> str:
    return _utc_iso(value)


def _write_report(vault: Path, report: dict) -> Path:
    import yaml  # noqa: F401  (确保依赖存在)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = vault / f"迁移报告-{stamp}.md"
    def table(counts: dict) -> str:
        lines = ["| 类型 | 数量 |", "|---|---|"]
        for key, value in counts.items():
            lines.append(f"| {key} | {value} |")
        return "\n".join(lines)

    lines = [
        "# Tbrain 迁移报告（正式执行）",
        "",
        f"- 时间：{report.get('started_at')} → {report.get('finished_at')}",
        f"- 旧应用数据目录：`{report.get('old_home')}`",
        f"- 旧 vault：`{report.get('old_vault')}`",
        f"- 新 vault：`{report.get('new_vault')}`",
        f"- 新 vault 迁移前备份：`{report.get('backup') or '无（新 vault 为空）'}`",
        "",
        "## 导入结果",
        table({
            "会话总结": report["imported"]["sessions"],
            "项目卡": report["imported"]["projects"],
            "公共池对象（新建）": report["imported"]["objects"],
            "公共池对象（取代旧卡）": report["updated"]["objects"],
            "全局提示词": report["imported"]["global_prompt"],
        }),
        "",
        "## 跳过（已存在或哈希未变）",
        table({
            "会话总结": report["skipped"]["sessions"],
            "项目卡": report["skipped"]["projects"],
            "公共池对象": report["skipped"]["objects"],
            "全局提示词": report["skipped"]["global_prompt"],
        }),
        "",
        "## 失败",
        "\n".join(f"- {item}" if isinstance(item, str) else
                  f"- {item.get('kind')}：{item.get('source')} → {item.get('error')}"
                  for item in report.get("failed") or []) or "- 无",
        "",
        "## 说明",
        "- 旧 vault、旧应用数据目录未改动；旧文件仍可在原位置读取。",
        "- 旧会话没有逐字原始命令与 U#：原始命令为空，全文保存在“旧总结原文（迁移）”节。",
        "- 迁移幂等：再次运行会按来源路径 + 内容哈希跳过已迁移内容。",
        "",
        "## 旧数据中的警告",
        "\n".join(f"- {item}" for item in report.get("warnings") or []) or "- 无",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
