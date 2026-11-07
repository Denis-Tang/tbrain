"""待接续决定交接包：Service、CLI 与 bootstrap 三处契约。"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from tbrain.service import TbrainService


def _prompt(service, session, cwd, text):
    service.hook("pi", {"hook_event_name": "UserPromptSubmit", "session_id": session,
                        "cwd": str(cwd), "prompt": text})


def _save(service, session, decisions, status=None):
    operations = [{"op": "append", "section": "decision",
                   "items": [{"text": text} for text in decisions]}]
    if status:
        operations.append({"op": "set_status", "status": status})
    return service.save(f"pi:{session}", operations)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("TBRAIN_HOME", str(home))
    service = TbrainService(home, vault=vault)
    service.settings.update({"vault_path": str(vault), "ntp_enabled": False})
    service.configure_project("甲项目", [str(repo)])
    criteria = service.vault_root() / "项目" / "甲项目" / "完成标准.md"
    text = criteria.read_text(encoding="utf-8")
    text = text.replace("（待填写：这个项目要做成什么样）", "交接包保持有效决定")
    text = text.replace("- [ ] （待填写：可以核对的完成条件）", "- [ ] 完成的会话仍能读到决定")
    criteria.write_text(text, encoding="utf-8")
    return service, repo, home


def test_handoff_pack_keeps_effective_decisions_after_session_completion(env):
    service, repo, _ = env
    _prompt(service, "a", repo, "定数据层结构")
    _save(service, "a", ["返回 dict，字段 ok/items | 接口层照抄字段名"])
    _prompt(service, "b", repo, "接口层")
    _save(service, "b", ["透传 Store 的 items | 不改字段名"])
    _prompt(service, "c", repo, "收尾")
    _save(service, "c", ["保持 Markdown 事实源 | 完成任务不应作废约定"], status="已完成")

    payload = service.handoff_pack("甲项目")
    assert payload["ok"] is True
    names = [item["session"] for item in payload["items"]]
    assert any(item['text'].startswith('保持 Markdown 事实源') for item in payload['items'])
    assert len(names) == 3
    assert all({"session", "id", "text", "at", "source"} <= set(item) for item in payload["items"])
    assert payload['items'][0]['text'].startswith('保持 Markdown 事实源')
    assert service.handoff_pack('甲项目', limit=1)['items'][0]['text'].startswith('保持 Markdown 事实源')


def test_handoff_pack_missing_project_and_bootstrap(env):
    service, repo, _ = env
    assert TbrainService.handoff_pack(service, "不存在的项目")["ok"] is False

    _prompt(service, "a", repo, "定数据层结构")
    _save(service, "a", ["返回 dict，字段 ok/items | 接口层照抄字段名"])
    _prompt(service, "b", repo, "换会话接手")
    created = service.bootstrap("pi:b2", "pi", str(repo))
    package = created["package"]
    assert "## 待接续决定" in package
    section = package.split("## 待接续决定", 1)[1]
    assert "- [[" in section.split("## ", 1)[0]


def test_handoff_filters_explicit_inactive_entries_and_survives_large_startup(env):
    service, repo, _ = env
    _prompt(service, 'a', repo, '保留有效规则')
    _save(service, 'a', ['禁用远程存储 | 用户要求', '旧实现 | 理由（被取代）',
                         '向量方案 | 尚未确认；建议，待用户确认'])
    service.save_global_prompt('很长的全局说明' * 3000)
    package = service.bootstrap('pi:large', 'pi', str(repo))['package']
    assert '禁用远程存储' in package
    assert '旧实现' not in package and '向量方案' not in package
    assert len(package) <= 12000
    assert service.handoff_pack('甲项目')['items'][0]['source'].endswith('#K1')


def test_bridge_cli_outputs_json(env):
    service, repo, home = env
    _prompt(service, "a", repo, "定数据层结构")
    _save(service, "a", ["返回 dict，字段 ok/items | 接口层照抄字段名"])
    proc = subprocess.run([sys.executable, "-m", "tbrain", "bridge", "show", "--project", "甲项目",
                           "--home", str(home)],
                          capture_output=True, text=True, encoding="utf-8", timeout=180)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True and payload["items"]
    bad = subprocess.run([sys.executable, "-m", "tbrain", "bridge", "show", "--project", "不存在的项目",
                          "--home", str(home)],
                         capture_output=True, text=True, encoding="utf-8", timeout=180)
    assert bad.returncode != 0


def test_handoff_pack_follows_project_tree_when_allowed(env):
    service, repo, _ = env
    child = repo / "child"
    child.mkdir()
    service.configure_project("甲项目", [str(repo)], allow_child=True)
    service.configure_project("子项目", [str(child)], parent="甲项目")
    _prompt(service, "c1", child, "子项目的活")
    _save(service, "c1", ["子项目返回 list | 与根项目区分"])

    payload = service.handoff_pack("甲项目")
    assert "子项目" in payload["scope"]
    owned = [item for item in payload["items"] if item.get("project") == "子项目"]
    assert owned and "子项目返回 list" in owned[0]["text"]

    service.configure_project("甲项目", [str(repo)], allow_child=False)
    hidden = service.handoff_pack("甲项目")
    assert all(item.get("project") != "子项目" for item in hidden["items"])
