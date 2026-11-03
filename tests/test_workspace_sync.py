import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tbrain.hosts import codex, config, dsh
from tbrain.service import TbrainService


def test_project_creation_syncs_connected_hosts_only(tmp_path, monkeypatch):
    brain = TbrainService(tmp_path / "home", vault=tmp_path / "vault")
    brain.settings.update({"vault_path": str(tmp_path / "vault"), "ntp_enabled": False})
    roots = [str(tmp_path / "one"), str(tmp_path / "two")]
    calls = []
    monkeypatch.setattr(codex, "create_workspace", lambda project: calls.append(project["paths"]) or "已创建项目")
    brain.configure_project("未接入", roots)
    assert calls == []
    for host in ("codex", "dsh", "pi"):
        config.write_connect_state(brain.home, host, {})
    saved = brain.configure_project("多目录", roots)
    assert calls == [saved["paths"]]
    assert len(saved["workspace_sync"]) == 3
    assert any("DSH 工作区只支持一个目录" in item for item in saved["workspace_sync"])
    assert any("Pi Desktop" in item for item in saved["workspace_sync"])
    brain.configure_project("改名", roots, rename_from="多目录")
    assert len(calls) == 1
    def offline(project):
        raise OSError("offline")

    monkeypatch.setattr(codex, "create_workspace", offline)
    saved = brain.configure_project("失败仍保存", roots)
    assert brain.store.project("失败仍保存")
    assert any("offline" in item for item in saved["workspace_sync"])


def test_codex_project_protocol(tmp_path, monkeypatch):
    driver = tmp_path / "codex.py"
    driver.write_text('''import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message["method"]
    if method == "initialized":
        continue
    if method == "initialize":
        assert message["params"]["capabilities"]["experimentalApi"]
        result = {}
    elif method == "project/list":
        result = {"data": [], "nextCursor": None}
    elif method == "project/create":
        assert message["params"]["name"] == "中文项目"
        assert len(message["params"]["roots"]) == 2
        result = {"project": {"id": "created"}}
    else:
        raise AssertionError(method)
    print(json.dumps({"id": message["id"], "result": result}), flush=True)
''', encoding="utf-8")
    popen = subprocess.Popen
    monkeypatch.setattr(codex.shutil, "which", lambda name: sys.executable)
    monkeypatch.setattr(codex.subprocess, "Popen", lambda command, **kwargs: popen([sys.executable, str(driver)], **kwargs))
    assert codex.create_workspace({"name": "中文项目", "paths": [str(tmp_path), str(tmp_path / "two")]}) == "已创建项目"


@pytest.mark.skipif(not shutil.which("node"), reason="Node required for DSH plugin")
def test_dsh_workspace_bridge(tmp_path):
    plugin = (Path(__file__).resolve().parents[1] / "integrations/tbrain-dsh.mjs").as_uri()
    driver = tmp_path / "dsh.mjs"
    driver.write_text('''import { apply } from "__PLUGIN__";
const handlers = {};
let workspace;
const ctx = {
  agents: { roots: () => [] },
  on: (name, handler) => { handlers[name] = handler; },
  workspaceController: {
    create: async ({path}) => {
      if (workspace) return { workspace, created: false };
      workspace = { id: "test", path };
      return { workspace, created: true };
    },
    rename: async ({workspaceId, title}) => {
      if (workspaceId !== "test") throw Error("wrong id");
      if (title === "conflict") throw Error("name conflict");
      workspace.title = title;
    },
    delete: async () => { workspace = undefined; },
  },
};
apply(ctx, { home: process.argv[2] });
process.stdin.resume();
process.stdin.on("data", async () => { await handlers.dispose(); process.exit(0); });
'''.replace("__PLUGIN__", plugin), encoding="utf-8")
    with subprocess.Popen(["node", str(driver), str(tmp_path)], stdin=subprocess.PIPE) as process:
        try:
            deadline = time.monotonic() + 5
            while not (tmp_path / "dsh-workspace-port").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            with pytest.raises(config.HostError, match="name conflict"):
                dsh.create_workspace({"name": "conflict", "paths": [str(tmp_path)]}, tmp_path)
            project = {"name": "中文项目", "paths": [str(tmp_path)]}
            assert dsh.create_workspace(project, tmp_path) == "已创建项目"
            assert dsh.create_workspace(project, tmp_path) == "项目已存在"
            process.communicate(b"stop", timeout=5)
            assert not (tmp_path / "dsh-workspace-port").exists()
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
