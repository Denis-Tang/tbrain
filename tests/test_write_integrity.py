"""Regressions for project metadata and concurrent writes, using temporary data only."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from tbrain import session as flow, store as store_module, worktree
from tbrain.service import TbrainService
from tbrain.settings import SettingsStore
from tbrain.store import render_note, split_front_matter


@pytest.fixture
def service(tmp_path, monkeypatch):
    for key, name in {
        'TBRAIN_HOME': 'home', 'TBRAIN_INSTALL_ROOT': 'install',
        'TBRAIN_SHORTCUT_DIRS': 'shortcuts', 'HOME': 'userhome', 'USERPROFILE': 'userhome',
        'CODEX_HOME': 'codex', 'PI_CODING_AGENT_DIR': 'pi',
        'CLAUDE_CONFIG_DIR': 'claude', 'DSH_HOME': 'dsh',
    }.items():
        monkeypatch.setenv(key, str(tmp_path / name))
    value = TbrainService(tmp_path / 'home', vault=tmp_path / 'vault')
    value.settings.save({'vault_path': str(tmp_path / 'vault'), 'ntp_enabled': False,
                         'timeout_hours': 0, 'task_mode': False})
    value.configure_project('项目甲', [])
    value.bootstrap('codex:write-integrity', 'codex', str(tmp_path), project='项目甲')
    return value


def test_vault_lock_preserves_an_existing_marker(service):
    path = service.store.vault / '.tbrain' / 'write.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'x')
    with service.store.write_lock():
        with service.store.write_lock():
            pass
    assert path.read_bytes() == b'x'


def test_timeout_fallback_preserves_concurrent_save(service, monkeypatch):
    summary, path = flow.load_session(service, 'codex:write-integrity')
    summary.header['last_activity_at'] = (service.now().utc - timedelta(hours=8)).strftime('%Y-%m-%dT%H:%M:%SZ')
    flow.write_session(service, summary, path)
    service.settings.save({'timeout_hours': 1})
    read_done, save_done = threading.Event(), threading.Event()
    original = flow.write_session

    def paused_write(current, snapshot, destination, **kwargs):
        if threading.current_thread().name == 'search-thread':
            read_done.set()
            save_done.wait(timeout=2)
        return original(current, snapshot, destination, **kwargs)

    monkeypatch.setattr(flow, 'write_session', paused_write)

    def search():
        threading.current_thread().name = 'search-thread'
        return service.search('合成查询', 'codex:write-integrity')

    def save():
        try:
            result = service.save('codex:write-integrity', [{
                'op': 'append', 'section': 'decision',
                'items': [{'text': 'B并发保存内容 | Event屏障验证'}],
            }])
            assert result['ok'], result
        finally:
            save_done.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(search)
        writer = None
        try:
            assert read_done.wait(5)
            writer = pool.submit(save)
            assert save_done.wait(5)
        finally:
            save_done.set()
        reader.result(5)
        if writer is not None:
            writer.result(5)
    assert 'B并发保存内容' in service.store.read_text(path)


def test_prepare_stays_outside_vault_lock(service, monkeypatch):
    """This invariant already passes before the fix."""
    entered, release, saved = threading.Event(), threading.Event(), threading.Event()

    def prepare(*args):
        entered.set()
        assert release.wait(5)
        return {}

    monkeypatch.setattr(worktree, 'prepare', prepare)
    service.settings.save({'task_mode': True})

    def save():
        result = service.save('codex:write-integrity', [{
            'op': 'append', 'section': 'decision',
            'items': [{'text': '锁外保存完成 | prepare不持库锁'}],
        }])
        assert result['ok'], result
        saved.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        configure = pool.submit(service.configure_project, '项目甲', [])
        writer = None
        try:
            assert entered.wait(5)
            writer = pool.submit(save)
            assert saved.wait(1)
        finally:
            release.set()
        configure.result(5)
        if writer is not None:
            writer.result(5)


@pytest.mark.parametrize('entry', ['store', 'service'])
def test_project_save_preserves_existing_metadata(service, entry):
    path = service.store.project_card_path('项目甲')
    data, body = split_front_matter(service.store.read_text(path))
    data.update({'worktree_branch': 'tbrain/项目甲', 'check_config': 'local.yaml',
                 'completion_criteria': '完成标准.md', 'x_unknown': {'keep': True}})
    body += '\n用户项目备注保留。\n'
    service.store.write_text(path, render_note(data, body))
    if entry == 'store':
        service.store.save_project('项目甲', [], allow_child=True)
    else:
        service.configure_project('项目甲', [], allow_child=True)
    after, after_body = split_front_matter(service.store.read_text(path))
    assert after == {**data, 'allow_child': True}
    assert after_body.strip() == body.strip()


def test_operation_oserror_reports_prior_writes_and_keeps_later_operations(service, monkeypatch):
    prompt_path = service.store.project_prompt_path('项目甲')
    original = os.replace

    def fail_prompt(source, target):
        if Path(target) == prompt_path:
            raise PermissionError('synthetic prompt write failure')
        return original(source, target)

    monkeypatch.setattr(os, 'replace', fail_prompt)
    monkeypatch.setattr(store_module.time, 'sleep', lambda _: None)
    result = service.save('codex:write-integrity', [
        {'op': 'env', 'items': [{'type': '工具', 'name': '合成工具', 'version': '1', 'state': '生效'}]},
        {'op': 'project_prompt', 'sections': {'最终目标': '合成目标'},
         'base_sha': {'最终目标': service.store.block_fingerprint(service.project_prompt('项目甲')['sections']['最终目标'])[:8]}},
        {'op': 'replace', 'section': 'next_step', 'text': '后续操作仍然保存，读取会话核对'},
    ])
    assert result['ok'] is False
    assert result['written']['env']
    assert result['written']['next_step'] == '后续操作仍然保存，读取会话核对'
    assert len(result['rejected']) == 1
    assert result['rejected'][0]['op'] == 'project_prompt'
    assert service.store.public_cards('工具')[0]['name'] == '合成工具'
    assert flow.load_session(service, 'codex:write-integrity')[0].next_step == result['written']['next_step']


@pytest.mark.parametrize('configured', [False, True])
def test_settings_processes_preserve_both_keys_without_vault_lock(tmp_path, configured):
    home = tmp_path / 'new-home'
    vault = tmp_path / 'vault'
    if configured:
        SettingsStore(home).save({'vault_path': str(vault)})
    child = r'''
from pathlib import Path
import os, sys
from tbrain import settings as module
home, key, value = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
real_replace = os.replace
def replace(source, target):
    (home / (key + '.tmp-name.txt')).write_text(str(source), encoding='utf-8')
    return real_replace(source, target)
module.os.replace = replace
print('ready', flush=True)
sys.stdin.buffer.read(1)
module.SettingsStore(home).save({key: int(value) if key == 'timeout_hours' else value})
'''
    processes = [subprocess.Popen(
        [sys.executable, '-I', '-c', child, str(home), key, value],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ) for key, value in [('timezone', 'Asia/Shanghai'), ('timeout_hours', '2')]]
    try:
        for process in processes:
            assert process.stdout.readline().strip() == b'ready'
        for process in processes:
            process.stdin.write(b'x')
            process.stdin.close()
            process.stdin = None
        results = [process.communicate(timeout=15) for process in processes]
        assert all(process.returncode == 0 for process in processes), results
    finally:
        for process in processes:
            if process.poll() is None:
                print(f'Stopping test child PID {process.pid}: {subprocess.list2cmdline(process.args)}')
                process.kill()
                process.communicate()
    data = json.loads((home / 'settings.json').read_text(encoding='utf-8'))
    assert data['timezone'] == 'Asia/Shanghai' and data['timeout_hours'] == 2
    assert not (home / 'settings.json').read_bytes().startswith(b'\xef\xbb\xbf')
    names = [(home / (key + '.tmp-name.txt')).read_text(encoding='utf-8')
             for key in ('timezone', 'timeout_hours')]
    assert len(set(names)) == 2
    assert not list(home.glob('*.tmp'))
    assert not (vault / '.tbrain' / 'write.lock').exists()
    if not configured:
        assert data['vault_path'] == '' and not vault.exists()


def test_project_and_session_writes_survive_100_aligned_rounds(service):
    card = service.store.project_card_path('项目甲')
    data, body = split_front_matter(service.store.read_text(card))
    data['x_unknown'] = 'keep'
    service.store.write_text(card, render_note(data, body))
    barrier = threading.Barrier(2)

    def configure(number):
        barrier.wait(timeout=5)
        service.configure_project('项目甲', [], allow_child=bool(number % 2))

    def save(number):
        barrier.wait(timeout=5)
        result = service.save('codex:write-integrity', [{
            'op': 'append', 'section': 'decision',
            'items': [{'text': f'并发压力决策{number} | 每轮独立保留'}],
        }])
        assert result['ok'], result

    with ThreadPoolExecutor(max_workers=2) as pool:
        for number in range(100):
            futures = (pool.submit(configure, number), pool.submit(save, number))
            for future in futures:
                future.result(30)
            after, _ = split_front_matter(service.store.read_text(card))
            assert after['x_unknown'] == 'keep'
            assert set(after) == set(data)
    summary, _ = flow.load_session(service, 'codex:write-integrity')
    decisions = [item['text'] for item in summary.all_items('决策')]
    assert len(decisions) == 100
    assert set(decisions) == {f'并发压力决策{number} | 每轮独立保留' for number in range(100)}


def test_vault_then_settings_lock_order_survives_100_aligned_rounds(service):
    store = service.store
    settings = SettingsStore(service.home)
    barrier = threading.Barrier(2)

    def vault_then_settings(number):
        barrier.wait(timeout=5)
        with store.write_lock():
            settings.save({'timeout_hours': number + 1})

    def save_settings(number):
        barrier.wait(timeout=5)
        service.save_settings({'timezone': 'UTC'})

    with ThreadPoolExecutor(max_workers=2) as pool:
        for number in range(100):
            futures = (pool.submit(vault_then_settings, number), pool.submit(save_settings, number))
            for future in futures:
                future.result(30)
    result = settings.load()
    assert result.timeout_hours == 100 and result.timezone == 'UTC'
