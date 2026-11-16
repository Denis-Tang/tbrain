import hashlib
import subprocess

from tbrain import handoff, prompt
from tbrain.index import Index
from tbrain.service import TbrainService
from tbrain.store import Store, export_project, render_note


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def hashes(folder):
    return {path.relative_to(folder).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in folder.rglob('*') if path.is_file()}


def test_twenty_projects_export_cold_without_writes_indexes_or_command_execution(tmp_path, monkeypatch):
    vault = tmp_path / 'vault'
    config = tmp_path / 'work' / '.tbrain' / 'check.yaml'
    write(config, 'checks:\n  - run: python -m pytest -q\n')
    (config.parent.parent / '.git').mkdir()
    write(vault / '项目' / '父项目' / '项目卡.md', render_note({'name': '父项目'}))
    write(vault / '项目' / '父项目' / '项目提示词.md', prompt.render({'硬规则': '- 保留父规则（来源：[[父来源]]#K1）'}))
    parent = handoff.new_summary(name='父来源', project='父项目', session_id='codex:parent', agent='codex',
        cwd='', host_record='', started_at='2026-11-12T00:00:00Z', raw_command='父项目要求')
    parent.sections['决策'] = [{'number': 1, 'text': '父规则已生效'}]
    write(vault / '会话总结' / '父项目' / '父来源.md', handoff.render(parent))
    for number in range(20):
        name = f'项目{number}'
        project_id = f'p-{number:012x}'
        card = {'name': name, 'project_id': project_id, 'parent': '父项目'}
        if number < 2:
            card['paths'] = str(config.parent.parent) if number == 0 else [str(config.parent.parent)]
        else:
            card['check_config'] = str(config)
        write(vault / '项目' / name / '项目卡.md', render_note(card))
        summary = handoff.new_summary(name=f'来源{number}', project=name, session_id=f'codex:{number}',
            agent='codex', cwd='', host_record='', started_at='2026-11-12T00:00:00Z', raw_command='项目要求')
        summary.sections['决策'] = [{'number': 1, 'text': '编码为 UTF-8 无 BOM'},
                                   {'number': 2, 'text': 'SQLite 是可重建索引'},
                                   {'number': 3, 'text': '执行 python -m pytest -q'},
                                   {'number': 4, 'text': '候选内容（建议，未确认）'},
                                   {'number': 5, 'text': '旧规则。已失效'}]
        write(vault / '会话总结' / name / f'来源{number}.md', handoff.render(summary))
        rules = '\n'.join([
            f'- 编码为 UTF-8 无 BOM（来源：[[来源{number}]]#K1）',
            f'- SQLite 是可重建索引（来源：[[来源{number}]]#K2）',
            f'- 执行 python -m pytest -q（来源：[[来源{number}]]#K3）',
            f'- 未确认内容原文（来源：[[来源{number}]]#K4）',
            f'- 已失效内容原文（来源：[[来源{number}]]#K5）',
            '- 失联内容原文（来源：[[已经找不到]]#U1）'])
        body = prompt.render({'最终目标': 'Tbrain 和 session_id 是合法业务技术词', '硬规则': rules,
                              '当前阶段': '导出：验证普通文档可用', '关键路径和命令': '\\## 原标题'},
                             user='用户手改补充\n\\## 用户标题')
        body = body.replace('## 硬规则\n', '## 硬规则〔sha:012345〕\n')
        if number == 0:
            body = body.replace('owner:user', 'owner: user').replace('owner:model', 'owner: model')
        write(vault / '项目' / name / '项目提示词.md', render_note({
            'body_encoding': 'escaped-headings', 'model_fingerprints': {'硬规则': 'private'}}, body))
        write(vault / '附件' / '资料' / f'{number}.md', render_note({
            'name': f'附件{number}', 'projects': [project_id], 'note': '附件正文不导出'}, 'private attachment body'))
    before = hashes(vault)

    def forbidden(*args, **kwargs):
        raise AssertionError('cold export must not construct an index/service/store or run commands')

    monkeypatch.setattr(Store, '__init__', forbidden)
    monkeypatch.setattr(Index, '__init__', forbidden)
    monkeypatch.setattr(TbrainService, '__init__', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    monkeypatch.chdir(tmp_path)
    for number in range(20):
        text = export_project(vault.relative_to(tmp_path) if number == 0 else vault, f'项目{number}')
        active, pending = text.split('## 来源待确认规则', 1)
        pending, inactive = pending.split('## 已失效规则（仅供参考）', 1)
        assert '未确认内容原文' not in active and '未确认内容原文' in pending
        assert '已失效内容原文' not in active and '已失效内容原文' in inactive
        assert '失联内容原文' in active and '来源待核对' in active
        assert '保留父规则' in active and '来源项目：父项目' in active
        assert f'附件{number}' in active and 'private attachment body' not in text
        assert 'UTF-8 无 BOM' in active and '可重建索引' in active
        assert 'python -m pytest -q' in active and str(config.parent.parent) in active
        assert 'Tbrain 和 session_id 是合法业务技术词' in text
        assert '## 原标题' in text and '## 用户标题' in text
        assert '来源说明：' in text and '用户手改补充' in text
        assert not text.encode('utf-8').startswith(b'\xef\xbb\xbf')
        assert all(token not in text for token in ('owner:', 'model_fingerprints:', 'session_id:', '[[', '\\##', '〔sha:'))
    assert hashes(vault) == before
    assert not list(tmp_path.rglob('index.sqlite3'))
