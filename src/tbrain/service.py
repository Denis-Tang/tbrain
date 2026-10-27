"""Tbrain service facade: settings, vault/index wiring, MCP tools, desktop API.

The flow semantics live in ``session.py``; this module owns lifecycle wiring and
the integration points used by the CLI, MCP server and desktop backend.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import threading
from zoneinfo import ZoneInfo, available_timezones

from . import __version__, clock, handoff, hooks, identity, session as flow, prompt as prompt_mod
from . import paths
from .settings import SettingsStore


class TbrainService:
    def __init__(self, home: Path | None = None, *, vault: Path | None = None, agent: str = ''):
        self.agent = agent
        self.home = paths.app_home(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self._settings = SettingsStore(self.home)
        self._vault_override = Path(vault) if vault else None
        self._store = None
        self._index = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 基础访问器
    # ------------------------------------------------------------------
    def version(self) -> str:
        return __version__

    @property
    def settings(self) -> SettingsStore:
        return self._settings

    def vault_root(self) -> Path:
        if self._vault_override is not None:
            return self._vault_override
        configured = str(self.settings.load().vault_path or "").strip()
        return Path(configured).expanduser() if configured else Path()

    def require_vault(self) -> Path:
        vault = self.vault_root()
        if not str(vault) or not vault.name:
            raise ValueError("请先在设置中选择知识库路径")
        return vault

    @property
    def store(self):
        with self._lock:
            if self._store is None:
                from .store import Store

                self._store = Store(self.require_vault(), self.home)
                self._store.ensure_layout()
            return self._store

    @property
    def index(self):
        with self._lock:
            if self._index is None:
                from .index import Index

                self._index = Index(self.home)
            return self._index

    def now(self):
        return clock.now(self.home, self.settings.load())

    # ------------------------------------------------------------------
    # 程序写入痕迹（用于检测用户手改）
    # ------------------------------------------------------------------
    def _writes_path(self) -> Path:
        return self.home / "writes.json"

    def _read_writes(self) -> dict:
        try:
            payload = json.loads(self._writes_path().read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def program_written_hash(self, path: Path) -> str:
        try:
            relative = Path(path).resolve().relative_to(self.require_vault().resolve()).as_posix()
        except (ValueError, OSError):
            relative = Path(path).as_posix()
        return str(self._read_writes().get(relative) or "")

    def remember_write(self, path: Path, text: str) -> None:
        try:
            relative = Path(path).resolve().relative_to(self.require_vault().resolve()).as_posix()
        except (ValueError, OSError):
            relative = Path(path).as_posix()
        payload = self._read_writes()
        payload[relative] = handoff.fingerprint(text)
        import uuid

        path = self._writes_path()
        temporary = path.with_name(f"writes.json.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        for attempt in range(3):
            try:
                temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                os.replace(temporary, path)
                return
            except OSError:
                if attempt == 2:
                    raise
                import time

                time.sleep(0.05 * (attempt + 1))
                payload = self._read_writes()
                payload[relative] = handoff.fingerprint(text)
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def reset_vault(self) -> None:
        with self._lock:
            self._store = None
            self._index = None

    # ------------------------------------------------------------------
    # 索引刷新
    # ------------------------------------------------------------------
    def refresh_index(self) -> None:
        vault = self.vault_root()
        if not str(vault) or not vault.is_dir():
            return
        store = self.store
        store.ensure_layout()
        index = self.index
        states = index.file_states()
        changed_projects = set()
        session_root = (vault / "会话总结")
        if session_root.is_dir():
            for path in session_root.rglob("*.md"):
                project = self._index_file(path, "session", vault, states)
                if project is not None:
                    changed_projects.add(project)
        for kind in ("安装", "环境变量", "PATH", "代理", "配置", "工作区", "工具"):
            folder = (vault / "公共池" / kind)
            if not folder.is_dir():
                continue
            for path in folder.glob("*.md"):
                self._index_file(path, "public", vault, states)
        for row in index.sessions():
            path = (vault / row["path"]) if row.get("path") else None
            if not path or not path.is_file():
                index.remove_session(row["session_id"])
                changed_projects.add(row["project"])
        for project in changed_projects:
            self.sync_session_index(project)

    def sync_session_index(self, project: str) -> None:
        # 读索引和发布 Markdown 共用写事务，较早的快照不能后写覆盖较新的索引。
        with self.index.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            rows = [dict(row) for row in con.execute("SELECT * FROM sessions WHERE project=? ORDER BY updated_at DESC, session_id", (project,))]
            rows.sort(key=lambda row: _priority(row['status']))
            if project:
                self.store.write_project_index(project, rows)
            else:
                self.store.write_independent_index(rows)

    def _index_file(self, path: Path, kind: str, vault: Path, states: dict) -> None:
        index = self.index
        try:
            stat = path.stat()
        except OSError:
            return
        relative = path.relative_to(vault).as_posix()
        state = states.get(relative)
        if state and float(state.get("mtime") or 0) == stat.st_mtime and int(state.get("size") or 0) == stat.st_size:
            return
        text = self.store.read_text(path)
        digest = handoff.fingerprint(text)
        if kind == "session":
            try:
                summary = handoff.parse(text)
            except Exception:
                return
            if not summary.session_id:
                return
            index.upsert_session(self._row_from_summary(summary, relative), text)
        else:
            front, body = _split_front(text)
            identifier = _public_id(relative, front)
            index.upsert_public({"id": identifier, "kind": str(front.get("kind") or path.parent.name),
                                 "name": str(front.get("name") or path.stem),
                                 "state": str(front.get("state") or "生效"),
                                 "path": relative, "updated_at": str(front.get("updated_at") or "")}, body)
        index.mark_scan(relative, stat.st_mtime, stat.st_size, digest)
        return summary.project if kind == "session" else None

    def _row_from_summary(self, summary: handoff.Summary, relative: str) -> dict:
        goals = summary.goals()
        return {
            "session_id": summary.session_id, "project": summary.project, "name": summary.name,
            "path": relative, "status": summary.status, "agent": str(summary.header.get("agent") or ""),
            "cwd": str(summary.header.get("cwd") or ""), "started_at": str(summary.header.get("started_at") or ""),
            "updated_at": str(summary.header.get("updated_at") or ""),
            "last_activity_at": str(summary.header.get("last_activity_at") or ""),
            "next_step": summary.next_step, "open_goals": sum(1 for item in goals if not item.get("done")),
            "git_branch": (summary.header.get("git") or {}).get("branch", ""),
            "git_head": (summary.header.get("git") or {}).get("head", ""),
            "git_dirty": int((summary.header.get("git") or {}).get("dirty") or 0),
            "last_verify": json.dumps(summary.header.get("last_verify") or {}, ensure_ascii=False),
            "file_hash": handoff.fingerprint(handoff.render(summary)),
            "file_mtime": 0.0,
        }

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def child_projects(self, project: str) -> list[str]:
        if not project:
            return []
        root = self.store.project(project)
        if not root or not root.get("allow_child"):
            return []
        return [item["name"] for item in self.store.projects() if item.get("parent") == project]

    def git_info(self, cwd: str) -> dict:
        info = {"branch": "", "head": "", "dirty": 0}
        if not cwd or not Path(cwd).is_dir():
            return info
        try:
            branch = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=2,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            head = subprocess.run(["git", "-C", cwd, "rev-parse", "HEAD"],
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=2,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            status = subprocess.run(["git", "-C", cwd, "status", "--porcelain"],
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=3,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if branch.returncode == 0:
                info["branch"] = branch.stdout.strip()
            if head.returncode == 0:
                info["head"] = head.stdout.strip()[:7]
            if status.returncode == 0:
                info["dirty"] = len([line for line in status.stdout.splitlines() if line.strip()])
        except (OSError, subprocess.SubprocessError):
            pass
        return info

    def version_warning(self) -> str:
        warnings: list[str] = []
        try:
            current_file = paths.current_file(paths.install_root(self.home, self.settings.load()))
            if current_file.is_file():
                installed = current_file.read_text(encoding="utf-8").strip()
                if installed and installed != __version__:
                    warnings.append(f"当前运行版本 {__version__} 与安装的 current（{installed}）不一致，请重启 Tbrain。")
        except OSError:
            pass
        try:
            from . import proc

            versions = proc.live_versions()
            older = sorted({v for v in versions if v and v != __version__})
            if older:
                warnings.append(f"检测到旧版本 Tbrain 进程（{', '.join(older)}），请重启 agent / 宿主以加载新版本 {__version__}。")
        except Exception:
            pass
        return "\n".join(warnings)

    # ------------------------------------------------------------------
    # hook
    # ------------------------------------------------------------------
    def hook(self, host: str, raw: dict) -> dict:
        from . import hosts
        if self._agent_disabled(identity.build_session_id(host, 'hook')):
            return {}

        raw = raw or {}
        event = hosts.normalize(host, raw)
        result = hooks.handle(event, self)
        raw_event = str(raw.get("hook_event_name") or raw.get("event") or "")
        return hosts.render(host, result, raw_event)

    # ------------------------------------------------------------------
    # 四个工具
    # ------------------------------------------------------------------
    def _agent_disabled(self, session_id: str) -> dict | None:
        from .hosts import config
        parts = identity.split_session_id(session_id)
        host = self.agent or (parts[0] if parts else '')
        if host and config.read_connect_state(self.home).get(host, {}).get('enabled') is False:
            return {'ok': False, 'error': 'Tbrain 在此 Agent 中已关闭，请在 Agent 管理页开启后重连。'}
        return None
    def bootstrap(self, session_id: str, agent: str = "", cwd: str = "", host_record: str = "",
                  project: str | None = None) -> dict:
        agent = agent or (identity.split_session_id(session_id) or ("unknown", ""))[0]
        cwd = cwd or os.getcwd()
        return self._agent_disabled(session_id) or flow.bootstrap(self, session_id=session_id, agent=agent, cwd=cwd,
                              host_record=host_record, project=project)

    def search(self, query: str, session_id: str = "", limit: int = 5) -> dict:
        return self._agent_disabled(session_id) or flow.search(self, query, session_id, limit)

    def save(self, session_id: str, operations: list[dict], last_test: dict | None = None) -> dict:
        return self._agent_disabled(session_id) or flow.save(self, session_id, operations, last_test)

    def resume(self, session_id: str, target: str, fork_user_count: int | None = None, fork_revision: str = "") -> dict:
        return self._agent_disabled(session_id) or flow.resume(self, session_id, target, fork_user_count=fork_user_count, fork_revision=fork_revision)

    def tool(self, name: str, arguments: dict | None = None) -> dict:
        arguments = arguments or {}
        if name == "bootstrap":
            return self.bootstrap(**{k: arguments[k] for k in arguments if k in
                                     ("session_id", "agent", "cwd", "host_record", "project")})
        if name == "search":
            return self.search(arguments.get("query", ""), arguments.get("session_id", ""),
                               arguments.get("limit", 5))
        if name == "save":
            return self.save(arguments.get("session_id", ""), arguments.get("operations") or [],
                             arguments.get("last_test"))
        if name == "resume":
            return self.resume(arguments.get("session_id", ""), arguments.get("target", ""),
                               arguments.get("fork_user_count"), arguments.get("fork_revision", ""))
        return {"ok": False, "error": f"未知工具：{name}"}

    # ------------------------------------------------------------------
    # 会话查询（桌面）
    # ------------------------------------------------------------------
    def sessions(self, status_filter: str = "all") -> list[dict]:
        self.refresh_index()
        rows = self.index.sessions()
        if status_filter and status_filter != "all":
            rows = [row for row in rows if row.get("status") == status_filter]
        rows.sort(key=lambda row: row.get("updated_at") or "", reverse=True)
        rows.sort(key=lambda row: _priority(row.get("status")))
        result = []
        for row in rows:
            result.append({
                "session_id": row.get("session_id"), "name": row.get("name"),
                "project": row.get("project") or "", "status": row.get("status"),
                "agent": row.get("agent"), "updated": row.get("updated_at") or "",
                "updated_at": row.get("updated_at"), "next_step": row.get("next_step") or "",
                "open_goals": row.get("open_goals") or 0, "path": row.get("path"),
            })
        return result

    def session_detail(self, path_or_id: str) -> dict:
        self.refresh_index()
        text = str(path_or_id or "")
        row = None
        if ":" in text:
            row = self.index.session(text)
        candidate = Path(text)
        if not candidate.is_absolute() and str(self.vault_root()):
            candidate = self.vault_root() / text
        if row is None and candidate.is_file():
            try:
                relative = candidate.resolve().relative_to(self.vault_root().resolve()).as_posix()
            except ValueError:
                relative = text
            for item in self.index.sessions():
                if item.get("path") == relative:
                    row = item
                    break
        if row is None:
            return {"ok": False, "error": "会话不存在"}
        path = self.vault_root() / row["path"]
        if not path.is_file():
            return {"ok": False, "error": "会话文件不存在"}
        return {"ok": True, "session": row, "markdown": self.store.read_text(path),
                "path": str(path), "name": row.get("name"), "updated": row.get("updated_at") or ""}

    def rename_session(self, path_or_id: str, new_name: str) -> dict:
        detail = self.session_detail(path_or_id)
        if not detail.get("ok"):
            return detail
        path = Path(detail["path"])
        summary = handoff.parse(self.store.read_text(path))
        new_path = flow.rename_session(self, summary, path, str(new_name or "").strip())
        return {"ok": True, "name": new_path.stem, "path": str(new_path)}

    # ------------------------------------------------------------------
    # 设置 / 时间 / 项目 / 提示词
    # ------------------------------------------------------------------
    def status(self) -> dict:
        values = self.settings.load()
        vault = self.vault_root()
        configured = bool(values.vault_path or self._vault_override)
        ready = configured and vault.is_dir()
        try:
            from .settings import available_timezones_list

            timezones = available_timezones_list()
        except Exception:
            timezones = sorted(available_timezones())
        try:
            current_time = clock.display(self.now()) if configured else ""
        except Exception:
            current_time = ""
        result = {
            "version": __version__,
            "ready": ready,
            "vault_path": str(vault) if configured else "",
            "settings": _settings_dict(values),
            "timezones": timezones,
            "current_time": current_time,
            "install_root": str(paths.install_root(self.home, values)),
            "current_version_on_disk": _installed_version(values),
            "vaults": [{"path": str(vault), "exists": vault.is_dir()}] if configured else [],
        }
        try:
            from . import installer

            notice = installer.pending_notice(self.home)
            if notice:
                result["pending_update"] = notice
        except Exception:
            pass
        return result

    def save_settings(self, values: dict) -> dict:
        if not isinstance(values, dict):
            raise ValueError("settings 必须是对象")
        self.settings.update(values)
        self.reset_vault()
        vault = self.vault_root()
        if str(vault) and vault.is_dir():
            self.store.ensure_layout()
        return {"ok": True, "settings": _settings_dict(self.settings.load())}

    def time_now(self) -> dict:
        now = self.now()
        return {
            "utc": now.utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "local": now.local.strftime("%Y-%m-%d %H:%M:%S"),
            "display": clock.display(now),
            "timezone": now.timezone,
            "calibrated": now.calibrated,
            "source": now.source,
            "offset_seconds": now.offset_seconds,
        }

    def projects(self) -> list[dict]:
        if not str(self.vault_root()):
            return []
        self.refresh_index()
        counts: dict[str, int] = {}
        for row in self.index.sessions():
            counts[row.get("project") or ""] = counts.get(row.get("project") or "", 0) + 1
        result = []
        for project in self.store.projects():
            result.append({**project, "sessions": counts.get(project["name"], 0),
                           "allow_child": bool(project.get("allow_child"))})
        return result

    def configure_project(self, name: str, paths_list: list[str], parent: str = "",
                          allow_child: bool = False, rename_from: str = "") -> dict:
        result = self.store.save_project(name, paths_list, parent=parent,
                                           allow_child=allow_child, rename_from=rename_from)
        self.refresh_index()
        return result

    def delete_project(self, name: str) -> dict:
        result = self.store.delete_project(name)
        self.refresh_index()
        return result

    def global_prompt(self) -> dict:
        return {"ok": True, "markdown": self.store.global_prompt()}

    def save_global_prompt(self, text: str) -> dict:
        return self.store.save_global_prompt(str(text or ""))

    def project_prompt(self, project: str) -> dict:
        data = self.store.project_prompt(str(project or ""))
        return {"ok": True, "project": project, "user": data.get("user") or "",
                "sections": data.get("sections") or prompt_mod.empty_sections()}

    def save_project_prompt(self, project: str, sections: dict) -> dict:
        result = self.store.save_project_prompt(str(project or ""), sections if isinstance(sections, dict) else {})
        result["ok"] = not result.get("rejected")
        return result

    def open_document(self, path: str) -> dict:
        candidate = Path(str(path or ""))
        if not candidate.is_file():
            candidate = self.vault_root() / str(path or "")
        if not candidate.is_file():
            return {"ok": False, "error": "文件不存在"}
        try:
            if os.name == "nt":
                os.startfile(str(candidate))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(candidate)])
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    # ------------------------------------------------------------------
    # 宿主与版本（委托给 hosts / installer / update）
    # ------------------------------------------------------------------
    def connect(self, host: str) -> dict:
        from . import hosts

        return hosts.connect(host, self.home, paths.install_root(self.home, self.settings.load()),
                             self.settings.load())

    def disconnect(self, host: str) -> dict:
        from . import hosts

        return hosts.disconnect(host, self.home, paths.install_root(self.home, self.settings.load()),
                                self.settings.load())

    def doctor(self) -> dict:
        from . import hosts

        report = hosts.doctor_all_hosts(self.home, paths.install_root(self.home, self.settings.load()),
                                        self.settings.load())
        report["version"] = __version__
        return report

    def check_update(self, force: bool = True) -> dict:
        from . import update

        return update.check_latest(self.home, force=force,
                                   install_root=paths.install_root(self.home, self.settings.load()))

    def apply_upgrade(self) -> dict:
        from . import hosts, update

        return update.upgrade(home=self.home,
                              install_root=paths.install_root(self.home, self.settings.load()),
                              force=True,
                              doctor=lambda: hosts.doctor_all_hosts(self.home, None, self.settings.load()))

    # ------------------------------------------------------------------
    # 迁移（第 6 步准备）
    # ------------------------------------------------------------------
    def migrate_dry_run(self, old_home: str) -> dict:
        old = Path(str(old_home or "")).expanduser()
        report = {
            "mode": "dry-run",
            "old_home": str(old),
            "old_home_exists": old.is_dir(),
            "old_vault": "",
            "old_vault_exists": False,
            "new_vault": str(self.vault_root()) if str(self.vault_root()) else "",
            "vault_files": {},
            "old_index": {},
            "note_kinds": {},
            "import_plan": [],
            "unmapped": [],
            "errors": [],
            "writes": "无（dry-run 只读）",
        }
        if not old.is_dir():
            report["errors"].append("旧数据目录不存在")
            return report
        settings_path = old / "settings.json"
        if settings_path.is_file():
            try:
                values = json.loads(settings_path.read_text(encoding="utf-8-sig"))
                report["old_vault"] = str(values.get("vault_path") or "")
            except (OSError, ValueError) as exc:
                report["errors"].append(f"settings.json 读取失败：{exc}")
        vault = Path(report["old_vault"]) if report["old_vault"] else None
        report["old_vault_exists"] = bool(vault and vault.is_dir())
        if vault and vault.is_dir():
            patterns = {
                "会话总结": "会话总结/**/*.md",
                "项目卡": "项目/*/项目.md",
                "知识": "知识/*.md",
                "技能": "技能/**/SKILL.md",
                "资料": "资料/**/*",
                "草稿": "草稿/**/*",
            }
            for label, pattern in patterns.items():
                try:
                    report["vault_files"][label] = sum(1 for _ in vault.glob(pattern))
                except OSError:
                    report["vault_files"][label] = 0
            report["vault_files"]["全局提示词"] = int((vault / "知识" / "全局提示词.md").is_file())
        for db in old.glob("index-*.sqlite3"):
            report["old_index"]["file"] = db.name
            try:
                import sqlite3

                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                tables = [row[0] for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name")]
                report["old_index"]["tables"] = tables
                counts = {}
                for table in tables:
                    try:
                        counts[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    except Exception:
                        continue
                report["old_index"]["counts"] = counts
                if "notes" in tables:
                    try:
                        report["note_kinds"] = {kind: count for kind, count in con.execute(
                            "SELECT kind, COUNT(*) FROM notes GROUP BY kind ORDER BY COUNT(*) DESC")}
                    except Exception:
                        pass
                con.close()
            except Exception as exc:
                report["errors"].append(f"读取旧索引 {db.name} 失败：{exc}")

        kinds = report.get("note_kinds") or {}
        report["import_plan"] = [
            {"source": "索引 notes(kind=session) / vault 会话总结", "count": kinds.get("session", 0),
             "target": "会话总结/<项目或独立>/YYYY/MM/<名称>.md",
             "action": "正文与 Markdown 结构保留；YAML 头按 handoff/v1 重建；旧 goal/progress/next_actions 映射到 目标/下一步；编号 D#/K#/E# 重建",
             "fields_mapped": ["原命令", "目标", "进度→已完成 D#", "下一步", "项目归属", "agent", "时间", "文件链接"]},
            {"source": "索引 notes(kind=project) / vault 项目/*/项目.md", "count": kinds.get("project", 0),
             "target": "项目/<名>/项目卡.md + 会话索引.md",
             "action": "名称、工作目录、父子关系、创建时间迁移；会话索引由程序重新生成",
             "fields_mapped": ["项目名", "路径列表", "父项目", "创建时间", "状态"]},
            {"source": "索引 notes(kind=object)", "count": kinds.get("object", 0),
             "target": "公共池/<类型>/<名称>.md",
             "action": "按旧对象的类型/名称重建公共池卡片；具体位置、当前状态、验证结果与简短历史保留",
             "fields_mapped": ["object", "action", "location", "state", "evidence", "conditions"]},
        ]
        report["unmapped"] = [
            {"source": "索引 notes(kind=memory)", "count": kinds.get("memory", 0),
             "reason": "旧“知识”属于维护轮产物；第一期不做维护轮，保留旧 vault 原样，不导入"},
            {"source": "索引 notes(kind=skill)", "count": kinds.get("skill", 0),
             "reason": "技能为维护轮产物；第一期不生成、不导入"},
            {"source": "索引 notes(kind=decision)", "count": kinds.get("decision", 0),
             "reason": "旧决策没有 handoff/v1 的来源与证据字段；需人工整理进项目提示词或会话总结"},
            {"source": "索引 notes(kind=task)", "count": kinds.get("task", 0),
             "reason": "旧任务卡模型已废弃；可人工转成会话总结的未完成目标 G#"},
            {"source": "索引 notes(kind=index) / processed / observations / daily_writes / state", "count": 0,
             "reason": "程序生成的索引、维护进度、访问统计与项目映射由新程序重建，不迁移"},
            {"source": "旧 error JSON / 费用账本 / 图表历史 / 附件注册记录", "count": 0,
             "reason": "第一期不做错误库与费用模块，保留在旧应用数据目录"},
        ]
        report["summary"] = {
            "会话总结": kinds.get("session", 0),
            "项目卡": kinds.get("project", 0),
            "公共池对象": kinds.get("object", 0),
            "不迁移（知识/技能/决策/任务等）": sum(kinds.get(key, 0) for key in ("memory", "skill", "decision", "task", "index")),
        }
        report["next_steps"] = [
            "确认后执行正式迁移：把计划中的会话总结、项目卡、公共池对象写入新 vault（旧 vault 只读、不移动）",
            "迁移前备份新 vault（若已存在）并生成逐文件报告与失败清单",
            "旧版解绑（改宿主全局配置）需另行确认后执行",
        ]
        return report

    def migrate(self, old_home: str, execute: bool = False, new_vault: Path | None = None) -> dict:
        from . import migrate as migrate_module

        target = Path(new_vault).expanduser() if new_vault else self.vault_root()
        old = Path(str(old_home or "")).expanduser()
        if not execute:
            return migrate_module.plan(old, target)
        if not str(target):
            return {"mode": "execute", "failed": ["未配置新 vault 路径（settings.vault_path 或 --to）"]}
        if new_vault is not None:
            self._vault_override = target
            self.reset_vault()
        return migrate_module.execute(self, old, target)


# ---------------------------------------------------------------------------
def _settings_dict(values) -> dict:
    try:
        from dataclasses import asdict

        return asdict(values)
    except TypeError:
        return {key: getattr(values, key, None) for key in
                ("vault_path", "timezone", "ntp_enabled", "timeout_hours",
                 "reminder_context_switch", "install_root")}


def _installed_version(settings) -> str:
    try:
        pointer = paths.current_file(paths.install_root(None, settings))
        if pointer.is_file():
            return pointer.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


def _priority(status) -> int:
    return {"已中断": 0, "进行中": 1, "受阻": 2, "待验证": 3, "已完成": 4, "已放弃": 5}.get(str(status), 5)


def _display(service, iso_text) -> str:
    try:
        moment = datetime.strptime(str(iso_text), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        zone = ZoneInfo(service.settings.load().timezone)
        return moment.astimezone(zone).strftime("%m-%d %H:%M")
    except Exception:
        return str(iso_text or "")


def _split_front(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    import yaml

    try:
        front = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError:
        front = {}
    return (front if isinstance(front, dict) else {}), text[end + 4:]


def _public_id(relative: str, front: dict) -> str:
    kind = str(front.get("kind") or Path(relative).parent.name)
    name = str(front.get("name") or Path(relative).stem)
    return f"{kind}-{name}"
