"""Project, standalone and public read boundaries in disposable vaults."""

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import sqlite3
import re

import pytest

from tbrain import handoff, session as flow
from tbrain.service import TbrainService


WORDS = [f"scope{number:02d}" for number in range(20)]
TEXT = " ".join(WORDS)


@pytest.fixture
def service(tmp_path, monkeypatch):
    for key, name in {'TBRAIN_HOME': 'home', 'TBRAIN_INSTALL_ROOT': 'install',
                      'TBRAIN_SHORTCUT_DIRS': 'shortcuts', 'HOME': 'userhome', 'USERPROFILE': 'userhome',
                      'CODEX_HOME': 'codex', 'PI_CODING_AGENT_DIR': 'pi',
                      'CLAUDE_CONFIG_DIR': 'claude', 'DSH_HOME': 'dsh'}.items():
        monkeypatch.setenv(key, str(tmp_path / name))
    value = TbrainService(tmp_path / 'home', vault=tmp_path / 'vault')
    value.settings.save({'vault_path': str(tmp_path / 'vault'), 'timezone': 'Asia/Shanghai',
                         'ntp_enabled': False, 'timeout_hours': 0})
    value.configure_project('项目甲', [])
    value.configure_project('项目乙', [])
    value.configure_project('项目丙', [], parent='项目甲')
    return value


def add(service, project, name, text=TEXT, *, host='codex'):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    sid = f'{host}:{project or "free"}-{name}'
    summary = handoff.new_summary(name=name, project=project, session_id=sid, agent=host,
        cwd=str(service.home / 'work'), host_record='', started_at=now.strftime('%Y-%m-%dT%H:%M:%SZ'), raw_command=text)
    path = service.store.session_path(project, now, name)
    flow.write_session(service, summary, path, create=True)
    return sid


@pytest.mark.parametrize('host', ['codex', 'pi'])
def test_twenty_queries_keep_projects_and_standalone_separate(service, host):
    caller = add(service, '项目甲', '当前会话', host=host)
    expected = add(service, '项目甲', '可见来源')
    add(service, '项目乙', '其他项目')
    add(service, '项目丙', '子项目')
    add(service, '', '非项目来源')
    service.store.save_public_card('工具', '合成公共工具', {'body': TEXT})
    for query in WORDS:
        results = service.search(query, caller)['results']
        sessions = [item for item in results if item['kind'] == 'session']
        assert [item['session_id'] for item in sessions] == [expected]
        assert caller not in {item.get('session_id') for item in results}
        assert any(item['source'] == '公共池' for item in results)


def test_scope_helpers_do_not_refresh_or_calibrate_and_do_not_inherit_switches(service, monkeypatch):
    caller = add(service, '项目甲', '甲调用者')
    child = add(service, '项目丙', '丙调用者')
    free = add(service, '', '独立调用者')
    service.configure_project('项目甲', [], allow_child=True)
    service.set_project_read_standalone('项目甲', True)
    def forbidden(*args, **kwargs):
        raise AssertionError('scope lookup must not refresh or calibrate')
    monkeypatch.setattr(service, 'refresh_index', forbidden)
    monkeypatch.setattr(service, 'now', forbidden)
    assert service.visible_projects(caller) == (['项目甲', '项目丙'], True)
    assert service.visible_projects(child) == (['项目丙'], False)
    assert service.visible_projects(free) == ([], True)
    assert service.visible_projects('codex:unknown') == ([], True)
    assert service.visible_projects_for_project('已删除项目') == (['已删除项目'], False)


@pytest.mark.parametrize('caller_kind', ['missing', 'unknown', 'standalone'])
def test_nonproject_search_keeps_public_then_standalone(service, caller_kind):
    add(service, '项目甲', '项目来源')
    source = add(service, '', '独立来源')
    caller = add(service, '', '独立调用者')
    service.store.save_public_card('工具', '公共来源', {'body': TEXT})
    sid = {'missing': '', 'unknown': 'codex:unknown', 'standalone': caller}[caller_kind]
    results = service.search(WORDS[0], sid)['results']
    assert results[0]['source'] == '公共池'
    assert source in {item.get('session_id') for item in results}
    assert all(not item['project'] for item in results if item['kind'] == 'session')
    if caller_kind == 'standalone':
        assert caller not in {item.get('session_id') for item in results}


@pytest.mark.parametrize('counts,expected', [((5, 2, 2), (4, 1, 0)),
                                           ((2, 0, 3), (2, 0, 1)),
                                           ((2, 3, 3), (2, 3, 0))])
@pytest.mark.parametrize('enabled', [False, True])
def test_project_search_pool_quotas(service, counts, expected, enabled):
    caller = add(service, '项目甲', '配额调用者', '')
    for number in range(counts[0]):
        add(service, '项目甲', f'项目来源{number}')
    for number in range(counts[1]):
        service.store.save_public_card('工具', f'公共来源{number}', {'body': TEXT})
    for number in range(counts[2]):
        add(service, '', f'独立来源{number}')
    service.set_project_read_standalone('项目甲', enabled)
    results = service.search(WORDS[0], caller)['results']
    actual = Counter(item['source'] for item in results)
    assert tuple(actual[name] for name in ('项目', '公共池', '独立会话池')) == (expected if enabled else (expected[0], expected[1], 0))
    assert [item['source'] for item in results] == sorted((item['source'] for item in results),
        key=('项目', '公共池', '独立会话池').index)


@pytest.mark.parametrize('limit', [1, 2, 3, 4])
def test_small_limits_and_aliases_exclude_the_canonical_current_session(service, limit):
    caller = add(service, '项目甲', '当前会话')
    add(service, '项目甲', '可见来源')
    add(service, '', '独立来源')
    service.store.save_public_card('工具', '公共来源', {'body': TEXT})
    service.index.register_link('codex', 'caller-alias', caller, str(service.home / 'work'))
    service.set_project_read_standalone('项目甲', True)
    results = service.search(WORDS[0], 'codex:caller-alias', limit)['results']
    assert len(results) <= limit
    assert caller not in {item.get('session_id') for item in results}
    assert all(item.get('project') in (None, '', '项目甲') for item in results)


def test_standalone_switch_changes_manual_reads_only(service):
    caller = add(service, '项目甲', '项目调用者', '')
    free = add(service, '', '非项目来源', 'standaloneonly')
    service.save(free, [{'op': 'append', 'section': 'decision', 'items': [{'text': '独立决定不自动注入 | 保持手动读取'}]}])
    assert service.search('standaloneonly', caller)['results'] == []
    assert not service.resume(caller, free)['ok']
    service.set_project_read_standalone('项目甲', True)
    assert service.search('standaloneonly', caller)['results'][0]['session_id'] == free
    package = service.bootstrap(caller, 'codex', str(service.home), project='项目甲')['package']
    assert '非项目来源' not in package and '独立决定不自动注入' not in package
    assert all(item.get('project') != '' for item in service.handoff_pack('项目甲')['items'])
    assert service.resume(caller, free)['ok']


@pytest.mark.parametrize('parent_enabled,child_enabled', [(True, False), (False, True)])
def test_parent_and_child_search_use_their_own_switch(service, parent_enabled, child_enabled):
    parent = add(service, '项目甲', '父调用者', '')
    child = add(service, '项目丙', '子调用者', '')
    source = add(service, '', '独立来源', 'standaloneonly')
    service.set_project_read_standalone('项目甲', parent_enabled)
    service.set_project_read_standalone('项目丙', child_enabled)
    for caller, enabled in ((parent, parent_enabled), (child, child_enabled)):
        results = service.search('standaloneonly', caller)['results']
        assert [item['session_id'] for item in results] == ([source] if enabled else [])


def test_resume_filters_invisible_names_before_ambiguity_and_never_loads_them(service, monkeypatch):
    caller = add(service, '项目甲', '调用者', '')
    allowed = add(service, '项目甲', '同名')
    forbidden = add(service, '项目乙', '同名')
    assert service.resume(caller, '同名')['resumed_session_id'] == allowed
    original = flow.load_session
    def checked(current, value):
        assert (value.get('session_id') if isinstance(value, dict) else value) != forbidden
        return original(current, value)
    monkeypatch.setattr(flow, 'load_session', checked)
    assert not service.resume(caller, forbidden)['ok']
    child = add(service, '项目丙', '同名')
    service.configure_project('项目甲', [], allow_child=True)
    result = service.resume(caller, '同名')
    assert not result['ok']
    assert {item['session_id'] for item in result['candidates']} == {allowed, child}


def test_deleted_project_retains_its_own_scope(service):
    service.configure_project('已删除项目', [])
    caller = add(service, '已删除项目', '调用者', '')
    expected = add(service, '已删除项目', '遗留来源')
    add(service, '', '独立来源')
    service.delete_project('已删除项目')
    assert service.visible_projects(caller) == (['已删除项目'], False)
    assert [item['session_id'] for item in service.search(WORDS[0], caller)['results']] == [expected]
    assert service.resume(caller, expected)['ok']


@pytest.mark.parametrize('old_project,new_project', [('', '项目甲'), ('项目甲', '项目乙')])
def test_bootstrap_reassignment_refreshes_old_and_new_markdown_indexes(service, old_project, new_project):
    sid = add(service, old_project, '归属迁移会话')
    original_path = service.index.session(sid)['path']
    assert service.bootstrap(sid, 'codex', str(service.home), project=new_project)['ok']
    assert service.index.session(sid)['path'] == original_path
    old_index = service.store.project_index_path(old_project) if old_project else service.store.independent_index_path()
    new_index = service.store.project_index_path(new_project)
    assert '[[归属迁移会话]]' not in old_index.read_text(encoding='utf-8')
    assert '[[归属迁移会话]]' in new_index.read_text(encoding='utf-8')
    result = service.bootstrap('codex:ghost', 'codex', str(service.home), project='不存在项目')
    assert not result['ok'] and service.index.session('codex:ghost') is None
    assert not service.store.project_dir('不存在项目').exists()


def test_internal_fork_inherits_current_header_despite_independent_directory(service):
    parent = add(service, '', '原独立来源', '父会话第一轮')
    saved = service.save(parent, [{'op': 'replace', 'section': 'target', 'text': '历史分叉点'}])
    revision = saved['revision']
    service.bootstrap(parent, 'codex', str(service.home), project='项目甲')
    parent_path = service.vault_root() / service.index.session(parent)['path']
    original = parent_path.read_bytes()
    assert '/独立/' in parent_path.as_posix()
    notice = service.hook('codex', {'hook_event_name': 'SessionStart', 'source': 'fork',
        'session_id': 'scope-fork', 'cwd': str(service.home / 'unmapped'),
        'parent_session_id': parent, 'fork_revision': revision})
    assert '历史分叉点' in str(notice)
    row = service.index.session('codex:scope-fork')
    assert row['project'] == '项目甲' and '/独立/' in row['path']
    assert parent_path.read_bytes() == original


def test_fork_does_not_overwrite_an_explicit_other_project(service):
    parent = add(service, '项目甲', '父来源', '不可跨项目继承')
    revision = service.save(parent, [{'op': 'replace', 'section': 'target', 'text': '父保存点'}])['revision']
    other = service.home / 'other-code'
    other.mkdir()
    service.configure_project('项目乙', [str(other)])
    notice = service.hook('codex', {'hook_event_name': 'SessionStart', 'source': 'fork',
        'session_id': 'other-project-fork', 'cwd': str(other), 'parent_session_id': parent, 'fork_revision': revision})
    assert '目标会话不在当前可见范围' in str(notice)
    assert service.index.session('codex:other-project-fork')['project'] == '项目乙'
    caller = add(service, '项目乙', '手动分叉', '')
    assert not service.resume(caller, parent, fork_revision=revision)['ok']


def test_codex_and_pi_share_project_search_and_resume_views(service):
    source = add(service, '项目甲', '共同来源')
    callers = [add(service, '项目甲', f'{host}调用者', '', host=host) for host in ('codex', 'pi')]
    service.save_project_prompt('项目甲', {'最终目标': '跨宿主项目规则'})
    payloads = [service.bootstrap(sid, host, str(service.home), project='项目甲')
                for sid, host in zip(callers, ('codex', 'pi'))]
    assert all(re.search(r'## 最终目标(?: 〔sha:[0-9a-f]{8}〕)?\n跨宿主项目规则\n', payload['package']) for payload in payloads)
    searches = [service.search(WORDS[0], sid)['results'] for sid in callers]
    assert [[item['session_id'] for item in hits] for hits in searches] == [[source], [source]]
    handoffs = [service.resume(sid, source)['handoff'] for sid in callers]
    assert handoffs[0] == handoffs[1]


@pytest.mark.parametrize('callsite', ['fts', 'like', 'public'])
def test_index_errors_log_only_metadata_in_app_home(service, monkeypatch, capsys, callsite):
    class BrokenConnection:
        def execute(self, *args):
            raise sqlite3.OperationalError('private-query-and-project-text')
    broken = BrokenConnection()
    @contextmanager
    def connect():
        yield broken
    index = service.index
    monkeypatch.setattr(index, 'connect', connect)
    capsys.readouterr()
    if callsite == 'fts':
        result = index._fts_session_hits(broken, 'secretneedle', ['项目甲'])
    elif callsite == 'like':
        result = index._like_session_ids(broken, '甲', ['项目甲'])
    else:
        result = index.search_public('secretneedle')
    assert result == []
    text = (service.home / 'tbrain.log').read_text(encoding='utf-8')
    assert len(text.splitlines()) == 1 and 'OperationalError' in text
    assert 'secretneedle' not in text and '项目甲' not in text and 'private-query' not in text
    captured = capsys.readouterr()
    assert captured.out == captured.err == ''


def test_log_io_error_does_not_break_search_or_write_protocol_streams(service, monkeypatch, capsys):
    from tbrain import index as index_module
    class BrokenConnection:
        def execute(self, *args):
            raise sqlite3.OperationalError('private query')
    def fail_log(*args, **kwargs):
        raise PermissionError('synthetic log IO failure')
    monkeypatch.setattr(index_module, 'RotatingFileHandler', fail_log)
    capsys.readouterr()
    assert service.index._fts_session_hits(BrokenConnection(), 'privatequery') == []
    captured = capsys.readouterr()
    assert captured.out == captured.err == ''
