"""完整启动包的正常矩阵与已观察到的超量输入。"""
from __future__ import annotations

import os
from pathlib import Path
import re
import runpy
import tempfile

import pytest

from tbrain import progress, prompt, session as flow
from tbrain.service import TbrainService


@pytest.fixture()
def case(tmp_path, monkeypatch):
    # 合成库构造器会重定向这些测试路径，测试结束恢复原环境。
    for key in ('TBRAIN_HOME', 'TBRAIN_INSTALL_ROOT', 'TBRAIN_SHORTCUT_DIRS', 'USERPROFILE',
                'HOME', 'CODEX_HOME', 'PI_CODING_AGENT_DIR', 'CLAUDE_CONFIG_DIR', 'DSH_HOME'):
        monkeypatch.setenv(key, os.environ.get(key, ''))
    make = runpy.run_path(str(Path(__file__).parent / 'fixtures' / 'make_vault.py'))['n1_case']

    def build(host='codex', mode=True, variant='F-A'):
        base = Path(tempfile.gettempdir()) / 'tbrain-dev-work'
        base.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix='b-', dir=base))
        manifest = make(root, host, mode, variant)
        service = TbrainService(Path(manifest['home']), vault=Path(manifest['vault']))
        return service, manifest

    return build


def check_package(service, manifest, package):
    assert len(package) <= flow.HOST_BUDGETS[manifest['host']]
    # 精确业务正文与下一节边界同时检查，防止维护说明混进第四节。
    tags = re.findall(r' 〔sha:[0-9a-f]{8}〕', package)
    assert len(tags) == 4 and sum(map(len, tags)) == 60
    for name, text in manifest['sections'].items():
        heading = re.search(r'^## ' + re.escape(name) + r'(?: 〔sha:[0-9a-f]{8}〕)?\n', package, re.MULTILINE)
        assert heading
        section = package[heading.end():].split('\n\n## ', 1)[0]
        assert section == text
    paths = re.findall(r'^\d+条；(.+)$', package, re.MULTILINE)
    assert paths
    for path in paths:
        assert Path(path.split('#', 1)[0]).exists(), path


@pytest.mark.parametrize('host', ['codex', 'pi'])
@pytest.mark.parametrize('mode', [False, True])
@pytest.mark.parametrize('resume', [False, True])
@pytest.mark.parametrize('variant', ['F-A', 'F-B'])
def test_startup_budget_matrix(case, host, mode, resume, variant):
    service, manifest = case(host, mode, variant)
    sid = manifest['session_id'] if resume else host + ':budget-new'
    package = service.bootstrap(sid, host, manifest['cwd'], project='项目甲')['package']
    check_package(service, manifest, package)
    assert ('## 工作约定' in package) == mode
    if resume:
        assert manifest['sections']['最终目标'] in package
        assert '下一步：' in package
        assert '最近 5 条用户指令' in package
    if variant == 'F-B':
        assert '待确认合成夹具' not in package


@pytest.mark.parametrize('host,pressure',
                         [(host, pressure) for host in ('codex', 'pi')
                          for pressure in ('user', 'error', 'limitation', 'first', 'paused', 'combined')] +
                         [('claude-code', 'combined'), ('dsh', 'combined')])
def test_startup_budget_pressure(case, host, pressure):
    service, manifest = case(host)
    sid = manifest['session_id']
    filler = '合成压力原文' * 4000
    if pressure in ('error', 'limitation', 'combined'):
        key = 'worktree_limitation' if pressure == 'limitation' else 'worktree_error'
        service.store.record_execution('项目甲', {key: filler[:20000]})
    if pressure in ('user', 'combined'):
        service.store.write_text(service.store.project_prompt_path('项目甲'),
                                 prompt.render(manifest['sections'], filler[:20000]))
    if pressure == 'first':
        sid = host + ':first-budget'
        service.bootstrap(sid, host, manifest['cwd'], project='项目甲')
        service.hook(host, {'hook_event_name': 'UserPromptSubmit', 'session_id': 'first-budget',
                            'cwd': manifest['cwd'], 'prompt': '合成任务'})
        fields = {key: (label + '合成事实' * 160)[:160] for key, label in progress.UNDERSTANDING_FIELDS}
        fields['scope'] = ('先说明核对来源的方法；旧任务目标：甲已废弃' + '合成事实' * 160)[:160]
        assert service.save(sid, [{'op': 'understanding', **fields},
                                  {'op': 'replace', 'section': 'next_step', 'text': '核对来源'}])['ok']
    if pressure in ('paused', 'combined'):
        assert service.save(sid, [{'op': 'append', 'section': 'done', 'items': [
            {'text': f'完成{n}→结果' + '合成证据' * 40} for n in range(15)]}])['ok']
        for _ in range(3):
            assert service.save(sid, [{'op': 'attempt', 'route': 'pytest -q exit 1',
                                      'evidence': '1 failed', 'productive': False}])['ok']
        assert service.save(sid, [{'op': 'replace', 'section': 'next_step',
                                  'text': '先核对失败证据，改用另一条路线并验证'}])['ok']
    if pressure == 'combined':
        (service.store.project_dir('项目甲') / '地图.md').write_text('# 合成事实地图\n', encoding='utf-8')
        conflicts = service.vault_root() / '公共池' / '冲突'
        conflicts.mkdir(parents=True, exist_ok=True)
        (conflicts / '合成.md').write_text('# 合成两版冲突\n', encoding='utf-8')
        (service.store.project_dir('项目甲') / '待用户确认.md').write_text(
            '## 合成业务待确认\n尚待选择的业务口径。\n\n## 写入冲突：合成章节\n旧版与新版。\n', encoding='utf-8')
    package = service.bootstrap(sid, host, manifest['cwd'], project='项目甲')['package']
    check_package(service, manifest, package)
    assert filler[:20000] not in package
    summary, _ = flow.load_session(service, sid)
    user_chars = sum(len(item.get('text', '')) for item in summary.users)
    assert f'完整用户原话正文：省略{user_chars}字' in package
    if pressure in ('user', 'error', 'limitation', 'combined'):
        assert '启动包省略清单' in package
    if pressure in ('error', 'limitation', 'combined'):
        assert '工作区说明正文：省略20000字' in package
        assert str(service.store.project_card_path('项目甲')) in package
    if pressure == 'first':
        assert all(value in package for value in fields.values())
        assert '\n下一步：核对来源\n' in package
        assert len(progress.lead_block(service, summary)) <= 750
    if pressure in ('paused', 'combined'):
        assert package.startswith('已暂停')
        assert '先核对失败证据，改用另一条路线并验证' in package
        assert '#热交接' in package
    if pressure == 'combined':
        line = next(line for line in package.splitlines() if line.startswith('写入冲突：'))
        assert len(line) <= 80 and '公共池1条' in line and '本项目1条' in line
        assert '## 本项目待用户确认\n2条；' in package
        assert '尚待选择的业务口径' not in package
        assert '尚待选择的业务口径' in (service.store.project_dir('项目甲') / '待用户确认.md').read_text(encoding='utf-8')
        line = next(line for line in package.splitlines() if line.startswith('记忆地图：'))
        assert len(line) <= 80
        assert str(service.store.project_prompt_path('项目甲')) in package
        assert (service.store.project_prompt_path('项目甲').parent / '地图.md').is_file()
        if host == 'codex':
            sections = {**manifest['sections'], '硬规则': '\n'.join(
                '- 用户手改长规则' + '合成原文' * 12 + '（来源：[[S0]]#K1）' for _ in range(150))}
            service.store.write_text(service.store.project_prompt_path('项目甲'), prompt.render(sections))
            oversized = service.bootstrap(sid, host, manifest['cwd'], project='项目甲')['package']
            assert len(oversized) <= flow.HOST_BUDGETS[host]
            assert '## 项目提示词维护' not in oversized
            assert '记忆地图：' not in oversized
            assert '记忆地图指针：省略' in oversized

