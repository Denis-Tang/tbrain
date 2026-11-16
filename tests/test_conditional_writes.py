import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tbrain import handoff, session as flow
from tbrain.service import TbrainService
from tbrain.store import Store, block_fingerprint, public_card_sha, split_front_matter


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'vault', tmp_path / 'home', now_fn=lambda: datetime(2026, 11, 13, tzinfo=timezone.utc))
    value.ensure_layout()
    value.save_project('P', [])
    return value


def test_project_conflicts_keep_both_versions_and_other_sections(store):
    store.save_project_prompt('P', {'最终目标': '共同旧目标'})
    base = block_fingerprint('共同旧目标')
    first = store.save_project_prompt('P', {'最终目标': '首次新目标'},
                                      base_sha={'最终目标': base}, session_id='codex:first')
    second = store.save_project_prompt('P', {'最终目标': '另一份草稿', '当前阶段': '仍可推进'},
                                       base_sha={'最终目标': base}, session_id='pi:second')
    assert first['written'] == {'最终目标': '首次新目标'}
    assert second['written'] == {'当前阶段': '仍可推进'}
    rejection = second['rejected'][0]
    assert rejection['conflict'] and rejection['base'] == base
    assert rejection['head'] == {'sha': block_fingerprint('首次新目标')[:8], 'text': '首次新目标'}
    pending = store.pending_confirmations('P')
    assert '首次新目标' in pending and '另一份草稿' in pending
    assert store.project_prompt('P')['sections']['最终目标'] == '首次新目标'
    store.save_project_prompt('P', {'最终目标': '用户照常保存'})
    assert store.project_prompt('P')['sections']['最终目标'] == '用户照常保存'
    rows = [json.loads(line) for line in (store.app_home / 'w3-writes.jsonl').read_text(encoding='utf-8').splitlines()]
    assert len(rows) == 5
    assert all(set(row) == {'at', 'session_id', 'target', 'has_base', 'result'} for row in rows)
    assert [(row['session_id'], row['result']) for row in rows[1:4]] == [
        ('codex:first', 'ok'), ('pi:second', 'conflict'), ('pi:second', 'ok')]


def test_missing_base_distinguishes_empty_heading_and_protects_user_edits(store):
    path = store.project_prompt_path('P')
    before = path.read_bytes()
    missing = store.save_project_prompt('P', {'最终目标': '不能覆盖空标题'}, base_sha={'最终目标': ''})
    assert missing['rejected'][0]['reason'] == '缺少 base_sha'
    assert missing['rejected'][0]['head']['text'] == ''
    assert path.read_bytes() == before
    path.write_text(path.read_text(encoding='utf-8').replace('## 最终目标\n', ''), encoding='utf-8')
    created = store.save_project_prompt('P', {'最终目标': '真正缺失节可建'}, base_sha={'最终目标': ''})
    assert created['written'] == {'最终目标': '真正缺失节可建'}
    path.write_text(path.read_text(encoding='utf-8').replace('真正缺失节可建', '用户手改'), encoding='utf-8')
    protected = store.save_project_prompt('P', {'最终目标': '不覆盖手改'}, base_sha={'最终目标': ''})
    assert '已被用户修改' in protected['rejected'][0]['reason']
    assert 'head' not in protected['rejected'][0]
    assert store.project_prompt('P')['sections']['最终目标'] == '用户手改'
    rows = [json.loads(line) for line in (store.app_home / 'w3-writes.jsonl').read_text(encoding='utf-8').splitlines()]
    assert [row['result'] for row in rows] == ['rejected', 'ok', 'rejected']


def test_twenty_independent_targets_and_twenty_writers_for_one_target(store):
    initial = {'最终目标': '旧目标', '硬规则': '- 保留原文（来源：#U1）',
               '当前阶段': '旧阶段', '关键路径和命令': 'python --version'}
    summaries = {}
    tasks = []
    for number in range(5):
        project = f'P{number}'
        store.save_project(project, [])
        summary = handoff.new_summary(name=f'来源{number}', project=project, session_id=f'codex:source{number}',
                                      agent='codex', cwd='', host_record='', started_at='2026-11-13T00:00:00Z',
                                      raw_command='保留原文')
        summaries[project] = summary
        store.save_project_prompt(project, initial, summary=summary)
        for section, old in store.project_prompt(project)['sections'].items():
            text = '- 并行保留（来源：#U1）' if section == '硬规则' else f'{section}并行新值'
            tasks.append((project, section, old, text))

    def write(item):
        project, section, old, text = item
        return store.save_project_prompt(project, {section: text}, summary=summaries[project],
                                         base_sha={section: block_fingerprint(old)[:8]})

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(write, tasks))
    assert len(results) == 20 and all(not result['rejected'] for result in results)
    for project in summaries:
        sections = store.project_prompt(project)['sections']
        assert all('并行' in value for value in sections.values())
    store.save_project_prompt('P', {'最终目标': '单节共同基准'})
    base = {'最终目标': block_fingerprint('单节共同基准')[:8]}
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda number: store.save_project_prompt(
            'P', {'最终目标': f'并发草稿{number}'}, base_sha=base), range(20)))
    assert sum(bool(result['written']) for result in results) == 1
    assert sum(bool(result['rejected']) for result in results) == 19
    assert store.pending_confirmations('P').count('写入冲突：') == 19


def test_public_conflict_is_separate_and_logs_each_target_once(store):
    card = store.save_public_card('工具', '合成工具', {'body': '共同原文', 'session_id': '业务字段'},
                                  session_id='codex:create')
    path = Path(card['path'])
    assert split_front_matter(path.read_text(encoding='utf-8'))[0]['session_id'] == '业务字段'
    base = public_card_sha(path.read_text(encoding='utf-8'))
    assert store.save_public_card('工具', '合成工具', {'body': '首次新文', 'base_sha': base},
                                  session_id='codex:first')['replaced']
    before = path.read_bytes()
    conflict = store.save_public_card('工具', '合成工具', {'body': '隔离草稿'}, base_sha=base, session_id='pi:second')
    assert conflict['conflict'] and path.read_bytes() == before
    conflict_text = Path(conflict['conflict_path']).read_text(encoding='utf-8')
    assert '首次新文' in conflict_text and '隔离草稿' in conflict_text
    assert conflict['head']['sha'] == public_card_sha(conflict['head']['text'])
    assert not (store.vault / '公共池' / '待用户确认.md').exists()
    assert store.public_cards('冲突') == []
    assert len(store.public_cards()) == 1 and '隔离草稿' not in store.public_l1()
    missing = store.save_public_card('工具', '合成工具', {'body': '漏条件'}, base_sha='', session_id='pi:missing')
    assert missing['reason'] == '缺少 base_sha' and path.read_bytes() == before
    assert 'base_sha' not in split_front_matter(path.read_text(encoding='utf-8'))[0]
    service = TbrainService(store.app_home, vault=store.vault)
    service.settings.update({'ntp_enabled': False})
    assert not service.search('隔离草稿')['results']
    conditions = Path(conflict['conflict_path']).stem + ' 合成工具'
    baseline = flow._dead_end_context(service, conditions, '', handoff.Summary(header={}))
    assert set(baseline['dependencies']) == {'合成工具'}
    rows = [json.loads(line) for line in (store.app_home / 'w3-writes.jsonl').read_text(encoding='utf-8').splitlines()]
    assert len(rows) == 3
    assert all(set(row) == {'at', 'session_id', 'target', 'has_base', 'result'} for row in rows)
    assert [row['result'] for row in rows] == ['ok', 'conflict', 'rejected']
    assert [row['has_base'] for row in rows] == [True, True, False]
    assert rows[0]['session_id'] == 'codex:first'
    assert rows[-1]['session_id'] == 'pi:missing'


@pytest.mark.parametrize('failure', ['after_archive', 'replace'])
def test_failed_public_replacement_preserves_current_file(store, monkeypatch, failure):
    card = store.save_public_card('工具', '保留现行', {'body': '可检索原文'})
    path = Path(card['path'])
    before = path.read_bytes()
    original_write = store.write_text
    original_replace = __import__('os').replace

    def write(target, text, **kwargs):
        original_write(target, text, **kwargs)
        if failure == 'after_archive' and Path(target).parent.name == '归档':
            raise OSError('合成归档后失败')

    def replace(source, target):
        if failure == 'replace' and Path(target) == path:
            assert Path(source).parent == path.parent and Path(source).suffix != '.md'
            raise OSError('合成replace失败')
        return original_replace(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(store, 'write_text', write)
        patch.setattr('tbrain.store.os.replace', replace)
        with pytest.raises(OSError):
            store.save_public_card('工具', '保留现行', {'body': '失败的新文'},
                                   base_sha=public_card_sha(path.read_text(encoding='utf-8')))
    assert path.read_bytes() == before
    assert split_front_matter(path.read_text(encoding='utf-8'))[0]['state'] == '生效'
    service = TbrainService(store.app_home, vault=store.vault)
    service.settings.update({'ntp_enabled': False})
    service.refresh_index()
    assert service.search('可检索原文')['results']
