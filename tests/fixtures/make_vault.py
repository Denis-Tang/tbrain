"""Build disposable base/N1/N3 fixtures; --out must be an empty TEMP directory."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile


def padded(prefix: str, length: int) -> str:
    return (prefix + "合成夹具用于检查项目记忆的完整性与来源。" * length)[:length]


def setup(root: Path):
    home, vault, work = (root / name for name in ("home", "vault", "work"))
    for path in (home, vault, work, root / "shortcuts", root / "userhome"):
        path.mkdir(parents=True, exist_ok=True)
    for key, path in {
        "TBRAIN_HOME": home, "TBRAIN_INSTALL_ROOT": root / "install",
        "TBRAIN_SHORTCUT_DIRS": root / "shortcuts", "USERPROFILE": root / "userhome",
        "HOME": root / "userhome", "CODEX_HOME": root / "host/codex",
        "PI_CODING_AGENT_DIR": root / "host/pi", "CLAUDE_CONFIG_DIR": root / "host/claude",
        "DSH_HOME": root / "host/dsh",
    }.items():
        os.environ[key] = str(path)
    from tbrain.service import TbrainService

    service = TbrainService(home, vault=vault)
    service.settings.update({"vault_path": str(vault), "timezone": "Asia/Shanghai", "ntp_enabled": False,
                             "timeout_hours": 0, "reminder_context_switch": True,
                             "install_root": str(root / "install")})
    return service, work


def save(service, sid: str, operations: list[dict]) -> dict:
    result = service.save(sid, operations)
    assert result["ok"], result["rejected"]
    return result


def message(service, sid: str, cwd: Path, text: str):
    host, raw_id = sid.split(":", 1)
    return service.hook(host, {"hook_event_name": "UserPromptSubmit", "session_id": raw_id,
                               "cwd": str(cwd), "prompt": text})


def session(service, sid: str, project: str, cwd: Path, name: str = "") -> dict:
    result = service.bootstrap(sid, sid.split(":", 1)[0], str(cwd), project=project)
    assert result["ok"], result
    if name:
        assert service.rename_session(sid, name)["ok"]
    return next(row for row in service.sessions() if row["session_id"] == sid)


def decision(service, sid: str):
    save(service, sid, [{"op": "append", "section": "decision", "items": [
        {"text": "SQLite 只做索引 | Markdown 是唯一事实源"}]}])


def finish_manifest(service, root: Path, **extra) -> dict:
    manifest = {"home": str(service.home), "vault": str(service.vault_root()), "work": str(root / "work"),
                "project_count": len(service.projects()), "session_count": len(service.sessions()),
                "attachment_count": len(service.store.attachments()),
                "public_card_count": len(service.store.public_cards()), **extra}
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def base(root: Path) -> dict:
    service, work = setup(root)
    for project, parent in (("项目甲", ""), ("项目乙", ""), ("项目丙", "项目甲")):
        service.configure_project(project, [], parent=parent)
    for project, count in (("项目甲", 3), ("项目乙", 3), ("项目丙", 2), ("", 5)):
        for number in range(count):
            sid = f"codex:base-{project or 'standalone'}-{number}"
            name = f"重名主题{number + 1}" if project in ("项目甲", "项目乙") and number < 2 else f"{project or '非项目'}会话{number + 1}"
            session(service, sid, project, work, name)
            save(service, sid, [{"op": "replace", "section": "target", "text": f"{project or '非项目'}记忆检索关键词{number + 1}"}])
    # bootstrap can change a session's project without moving its original file.
    session(service, "codex:base-moved", "", work, "目录与归属背离")
    service.bootstrap("codex:base-moved", "codex", str(work), project="项目甲")
    service.configure_project("已删除项目", [])
    session(service, "codex:base-deleted", "已删除项目", work, "遗留会话")
    service.delete_project("已删除项目")
    # Attachments have no public write API in 0.7.3.
    from tbrain.store import render_note

    for project in ("项目甲", "项目乙"):
        folder = service.vault_root() / "附件" / "文档"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{project}附件.md").write_text(render_note(
            {"name": f"{project}附件", "type": "文档", "projects": [project], "path": str(work / "说明.md")},
            "合成附件说明"), encoding="utf-8")
    for number in range(5):
        service.store.save_public_card("工具", f"合成工具{number + 1}", {"version": "1", "state": "生效"})
    return finish_manifest(service, root, profile="base", duplicate_groups=2,
                           mismatched_session="codex:base-moved", deleted_session="codex:base-deleted")


def n1_case(root: Path, host: str, variant: str) -> dict:
    service, work = setup(root)
    folder = work / "项目甲"
    folder.mkdir()
    service.configure_project("项目甲", [str(folder)])
    session(service, "codex:n1-source", "项目甲", folder, "S0")
    decision(service, "codex:n1-source")
    save(service, "codex:n1-source", [{"op": "set_status", "status": "已完成"}])
    paths = []
    for number in range(10):
        path = folder / f"f{number}.txt"
        path.write_text("合成内容\n", encoding="utf-8")
        paths.append(f"- 验证路径{number}：{path}")
    sections = {
        "最终目标": "\n".join(padded(f"目标{n}：", 75) for n in range(3)),
        "硬规则": "\n".join(f"- {padded(f'规则{n}：', 62)}（来源：[[S0]]#K1）" for n in range(12)),
        "当前阶段": "\n".join(padded(f"阶段{n}：", 75) for n in range(5)),
        "关键路径和命令": "\n".join(paths),
    }
    result = service.save_project_prompt("项目甲", sections)
    assert result["ok"], result
    assert service.project_prompt("项目甲")["sections"] == sections
    if variant == "F-B":
        (service.store.project_dir("项目甲") / "待用户确认.md").write_text(padded("待确认", 1700), encoding="utf-8")
    sid = f"{host}:n1-probe"
    session(service, sid, "项目甲", folder)
    prompts = [padded("第一条合成任务指令", 300), "确认"]
    prompts.extend(padded(f"合成指令{n}", 300) for n in range(4))
    prompts.append("按既定范围继续核对合成项目记忆")
    if variant == "F-B":
        prompts.extend(padded(f"最坏输入指令{n}", 800) for n in range(5))
    for text in prompts:
        message(service, sid, folder, text)
    save(service, sid, [{"op": "replace", "section": "target", "text": padded("合成目标", 200)},
                        {"op": "replace", "section": "next_step", "text": padded("运行合成检查验证", 150)},
                        {"op": "append", "section": "done", "items": [{"text": padded(f"完成合成步骤{n}→结果", 200)} for n in range(5)]},
                        {"op": "append", "section": "goal", "items": [{"text": padded(f"待办{n}", 200)} for n in range(3)]}])
    return finish_manifest(service, root, profile="n1", host=host, variant=variant,
                           session_id=sid, project="项目甲", cwd=str(folder), sections=sections,
                           user_lengths=[len(text) for text in prompts])


def n1(root: Path) -> dict:
    cases = []
    for host in ("codex", "pi"):
        for variant in ("F-A", "F-B"):
            case = root / f"{host}-memory-{variant}"
            cases.append(n1_case(case, host, variant))
    manifest = {"profile": "n1", "cases": cases}
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def n3(root: Path) -> dict:
    service, work = setup(root)
    folder = work / "项目甲"
    folder.mkdir()
    service.configure_project("项目甲", [str(folder)])
    source = session(service, "codex:n3src", "项目甲", folder)
    decision(service, "codex:n3src")
    control = session(service, "codex:n3control", "项目甲", folder, "S1")
    decision(service, "codex:n3control")
    sections = {"硬规则": "\n".join([
        f"- 原始文本保持完整（来源：[[{source['name']}]]#K1）",
        f"- 索引允许重建（来源：[[{source['name']}]]#K1）",
        f"- 对照规则保持生效（来源：[[{control['name']}]]#K1）",
    ])}
    result = service.save_project_prompt("项目甲", sections)
    assert result["ok"], result
    assert len(service.project_prompt("项目甲")["sections"]["硬规则"].splitlines()) == 3
    return finish_manifest(service, root, profile="n3", source_id="codex:n3src", source_name=source["name"],
                           control_id="codex:n3control", cwd=str(folder), project="项目甲",
                           rename_event={"hook_event_name": "UserPromptSubmit", "session_id": "n3src",
                                         "cwd": str(folder), "prompt": "整理项目甲的索引重建方案并补充回归测试"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--profile", choices=("base", "n1", "n3"), default="base")
    args = parser.parse_args()
    root = args.out.resolve()
    temporary = Path(tempfile.gettempdir()).resolve()
    if root == temporary or not root.is_relative_to(temporary):
        parser.error("--out must be below TEMP")
    if root.exists() and any(root.iterdir()):
        parser.error("--out must be empty or absent")
    root.mkdir(parents=True, exist_ok=True)
    manifest = {"base": base, "n1": n1, "n3": n3}[args.profile](root)
    print(json.dumps({"manifest": str(root / "manifest.json"), "profile": args.profile,
                      **{key: manifest[key] for key in ("project_count", "session_count", "attachment_count", "public_card_count") if key in manifest},
                      **({"case_count": len(manifest["cases"])} if "cases" in manifest else {})}, ensure_ascii=False))


if __name__ == "__main__":
    main()
