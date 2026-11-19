import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from tbrain.store import Store, split_front_matter


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'vault', tmp_path / 'home', now_fn=lambda: datetime(2026, 11, 13, tzinfo=timezone.utc))
    value.ensure_layout()
    value.save_project('P', [])
    return value


def test_decision_log_combined_limit_and_optional_reason(store):
    first = store.append_decision_log('P', '文' * 100, '因' * 100, 'user', 'codex:decision', 'U8')
    path = store.project_dir('P') / '决策日志.md'
    before = path.read_bytes()
    assert first['id'] == 'DL-0001'
    assert split_front_matter(before.decode('utf-8'))[0]['project_id'] == store.project('P')['project_id']
    assert '用户不手改' in before.decode('utf-8') and 'source: U8' in before.decode('utf-8')
    for text, why in [('', '原因'), ('文' * 100, '因' * 101)]:
        with pytest.raises(ValueError):
            store.append_decision_log('P', text, why, 'user', 'codex:decision')
        assert path.read_bytes() == before


def test_save_decision_is_project_scoped_and_available_after_restart(tmp_path):
    from tbrain.service import TbrainService

    work = tmp_path / 'work'
    work.mkdir()
    home, vault = tmp_path / 'home', tmp_path / 'vault'
    service = TbrainService(home, vault=vault)
    service.settings.update({'vault_path': str(vault), 'ntp_enabled': False})
    service.store.save_project('P', [str(work)])
    service.store.save_project('Other', [])
    response = service.hook('codex', {'hook_event_name': 'UserPromptSubmit', 'session_id': 'decision',
                                      'cwd': str(work), 'prompt': '以后默认离线保存，用户数据不上传'})
    assert '当前用户原话编号：U1' in response['hookSpecificOutput']['additionalContext']
    result = service.tool('save_decision', {'session_id': 'codex:decision',
        'text': '默认离线保存，用户数据不上传', 'source': ['U1']})
    assert result['ok'], result
    assert result['written']['id'] == 'DL-0001'
    assert result['written']['why'] == ''
    assert result['written']['source'] == 'U1'
    assert result['written']['source_text'] == 'U1: 以后默认离线保存，用户数据不上传'
    stop = service.hook('codex', {'hook_event_name': 'Stop', 'session_id': 'decision', 'cwd': str(work)})
    assert stop.get('decision') != 'block'
    assert not (service.store.project_dir('Other') / '决策日志.md').exists()
    restarted = TbrainService(home, vault=vault)
    package = restarted.bootstrap('codex:fresh', 'codex', str(work))['package']
    assert 'DL-0001 默认离线保存，用户数据不上传' in package
    hits = restarted.search('离线', 'codex:fresh')['results']
    assert hits[0]['kind'] == 'decision' and hits[0]['text'] == result['written']['text']
    assert hits[0]['source'] == 'U1'
    full = restarted.read('codex:fresh', **hits[0]['read'])
    assert full['ok'] and 'U1: 以后默认离线保存，用户数据不上传' in full['content']
    assert 'source_text' not in hits[0]  # discovery stays small; read carries the full quote
    assert 'DL-0001 默认离线保存' in restarted.resume('codex:fresh', 'codex:decision')['handoff']
    restarted.bootstrap('codex:other', 'codex', str(work), project='Other')
    assert not any(hit['kind'] == 'decision' for hit in restarted.search('离线', 'codex:other')['results'])


def test_long_term_decisions_inherit_parent_scope(tmp_path):
    from tbrain.service import TbrainService

    work = tmp_path / 'work'
    work.mkdir()
    service = TbrainService(tmp_path / 'home', vault=tmp_path / 'vault')
    service.settings.update({'vault_path': str(tmp_path / 'vault'), 'ntp_enabled': False})
    service.store.save_project('Parent', [])
    service.store.save_project('Child', [str(work)], parent='Parent')
    service.bootstrap('codex:parent', 'codex', str(work), project='Parent')
    service.hook('codex', {'hook_event_name': 'UserPromptSubmit', 'session_id': 'parent',
                           'cwd': str(work), 'prompt': '所有项目数据离线保存'})
    assert service.save('codex:parent', [{'op': 'append', 'section': 'decision',
        'items': [{'text': '父项目本次技术路线 | 仅本次使用'}]}])['ok']
    assert service.save_decision('codex:parent', '所有项目数据离线保存', ['U1'])['ok']
    package = service.bootstrap('codex:child', 'codex', str(work))['package']
    assert '项目长期决定：Parent' in package and '所有项目数据离线保存' in package
    assert service.search('离线', 'codex:child')['results'][0]['project'] == 'Parent'
    handed = service.handoff_pack('Child')['items']
    assert [entry['id'] for entry in handed] == ['DL-0001']
    service.bootstrap('codex:child-fresh', 'codex', str(work))
    assert '所有项目数据离线保存' in service.resume('codex:child-fresh', 'codex:child')['handoff']


def test_startup_omits_whole_decisions_and_points_to_the_log(store, tmp_path):
    from tbrain.service import TbrainService
    service = TbrainService(tmp_path / 'home', vault=store.vault)
    service.settings.save({'ntp_enabled': False})
    store.save_project('P', [])
    for n in range(9):
        store.append_decision_log('P', f'决定{n} ' + '适用条件。' * 28 + '\n仅在试运行期间', '', '用户', 'codex:old')
    package = service.bootstrap('codex:reader', 'codex', str(tmp_path), project='P')['package']
    shown = sum(entry['text'] in package for entry in store.project_decisions('P'))
    assert 0 < shown < 9
    assert package.count('DL-') == shown
    assert f'另有{9-shown}条；{store.project_dir("P") / "决策日志.md"}' in package


def test_save_decision_requires_real_source_and_preserves_replaced_history(tmp_path):
    from tbrain.service import TbrainService

    work = tmp_path / 'work'
    work.mkdir()
    service = TbrainService(tmp_path / 'home', vault=tmp_path / 'vault')
    service.settings.update({'vault_path': str(tmp_path / 'vault'), 'ntp_enabled': False})
    service.store.save_project('P', [str(work)])
    payload = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'decision', 'cwd': str(work)}
    service.hook('codex', {**payload, 'prompt': '长期使用旧格式保存'})
    assert not service.save_decision('codex:decision', '伪来源', ['U9'])['ok']
    assert not service.save_decision('codex:decision', '空来源', [])['ok']
    assert not (service.store.project_dir('P') / '决策日志.md').exists()
    first = service.save_decision('codex:decision', '长期使用旧格式保存', ['U1'])
    assert first['ok'], first
    before = (service.store.project_dir('P') / '决策日志.md').read_bytes()
    assert not service.save_decision('codex:decision', '错误替代', ['U1'], supersedes=['DL-0099'])['ok']
    assert (service.store.project_dir('P') / '决策日志.md').read_bytes() == before
    response = service.hook('codex', {**payload, 'prompt': '改为长期使用新格式保存，旧格式不再使用'})
    assert '当前用户原话编号：U2' in response['hookSpecificOutput']['additionalContext']
    second = service.save_decision('codex:decision', '长期使用新格式保存', ['U2'], supersedes=['DL-0001'])
    assert second['ok'], second
    history = service.store.project_decisions('P')
    assert [entry['active'] for entry in history] == [False, True]
    assert history[0]['text'] == '长期使用旧格式保存'
    package = service.bootstrap('codex:fresh', 'codex', str(work))['package']
    assert 'DL-0002 长期使用新格式保存' in package
    assert 'DL-0001 长期使用旧格式保存' not in package
    hits = service.search('旧格式', 'codex:fresh')['results']
    assert any(hit['kind'] == 'decision' and hit['snippet'].startswith('[已被替代]') for hit in hits)


@pytest.mark.parametrize('workers', ['threads', 'processes'])
def test_decision_log_concurrent_numbering(store, workers):
    if workers == 'threads':
        with ThreadPoolExecutor(max_workers=20) as pool:
            list(pool.map(lambda number: store.append_decision_log(
                'P', f'决策{number}', f'理由{number}', 'user', f'codex:thread{number}'), range(20)))
    else:
        code = '''
from pathlib import Path
from datetime import datetime, timezone
import sys
from tbrain.store import Store
s = Store(Path(sys.argv[1]), Path(sys.argv[2]), now_fn=lambda: datetime(2026, 11, 13, tzinfo=timezone.utc))
for number in range(5):
    s.append_decision_log('P', f'决策{sys.argv[3]}-{number}', f'理由{number}', 'user', f'pi:process{sys.argv[3]}')
'''
        children = [subprocess.Popen([sys.executable, '-c', code, str(store.vault),
                                      str(store.app_home / f'worker{number}'), str(number)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for number in range(4)]
        for child in children:
            out, err = child.communicate(timeout=30)
            assert child.returncode == 0, out + err
    text = (store.project_dir('P') / '决策日志.md').read_text(encoding='utf-8')
    assert re.findall(r'^## (DL-\d+) —', text, re.MULTILINE) == [f'DL-{number:04d}' for number in range(1, 21)]
    assert text.count('\ntext: 决策') == text.count('\nwhy: 理由') == 20
