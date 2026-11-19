from datetime import datetime, timezone

import pytest

from tbrain import handoff, prompt, session as flow
from tbrain.service import TbrainService
from tbrain.store import render_note, split_front_matter


@pytest.fixture
def service(tmp_path, monkeypatch):
    for key in ('TBRAIN_HOME', 'TBRAIN_INSTALL_ROOT', 'TBRAIN_SHORTCUT_DIRS', 'HOME', 'USERPROFILE',
                'CODEX_HOME', 'PI_CODING_AGENT_DIR', 'CLAUDE_CONFIG_DIR', 'DSH_HOME'):
        monkeypatch.setenv(key, str(tmp_path / key))
    value = TbrainService(tmp_path / 'home', vault=tmp_path / 'vault')
    value.settings.save({'vault_path': str(tmp_path / 'vault'), 'ntp_enabled': False,
                         'timeout_hours': 0})
    value.configure_project('项目甲', [])
    value.configure_project('项目乙', [])
    return value


def source(service, project, name, *, decision='保持 UTF-8 无 BOM | 用户要求', aliases=(), provisional=False):
    now = datetime(2026, 11, 12, tzinfo=timezone.utc)
    sid = f'codex:{project}-{name}'
    summary = handoff.new_summary(name=name, project=project, session_id=sid, agent='codex',
        cwd=str(service.home), host_record='', started_at='2026-11-12T00:00:00Z',
        raw_command='你好' if provisional else '保存项目约定', provisional=provisional)
    summary.sections['决策'] = [{'number': 1, 'text': decision}]
    if aliases:
        summary.header['aliases'] = list(aliases)
    path = service.store.session_path(project, now, name)
    flow.write_session(service, summary, path)
    return sid, path


@pytest.mark.parametrize('ownership', ['fingerprinted', 'without_fingerprints', 'user_edited'])
@pytest.mark.parametrize('route', ['manual', 'automatic', 'resume', 'chain'])
def test_renaming_keeps_original_rule_and_prompt_bytes(service, ownership, route):
    sid, old_path = source(service, '项目甲', '原来源', provisional=route in {'manual', 'automatic', 'resume'})
    rule = '- 使用 UTF-8 无 BOM（来源：[[原来源]]#K1）'
    assert not service.store.save_project_prompt('项目甲', {'硬规则': rule})['rejected']
    path = service.store.project_prompt_path('项目甲')
    if ownership == 'without_fingerprints':
        front, body = split_front_matter(service.store.read_text(path))
        front.pop('model_fingerprints', None)
        service.store.write_text(path, render_note(front, body))
    elif ownership == 'user_edited':
        service.store.write_text(path, service.store.read_text(path).replace('使用 UTF-8', '用户坚持 UTF-8'))
        rule = rule.replace('使用 UTF-8', '用户坚持 UTF-8')
    before = path.read_bytes()
    if route == 'automatic':
        text = '修复项目索引并验证旧规则能够继续使用'
        service.hook('codex', {'hook_event_name': 'UserPromptSubmit', 'session_id': sid.split(':', 1)[1],
                               'cwd': str(service.home), 'prompt': text})
    elif route == 'resume':
        target, _ = source(service, '项目甲', '接续目标')
        assert service.resume(sid, target)['ok']
    else:
        service.rename_session(sid, '中间名称' if route == 'chain' else '新的来源')
        if route == 'chain':
            service.rename_session(sid, '最终名称')
        else:
            text = '首条消息不能覆盖已经手动设置的会话名称'
            service.hook('codex', {'hook_event_name': 'UserPromptSubmit', 'session_id': sid.split(':', 1)[1],
                                   'cwd': str(service.home), 'prompt': text})
    summary, renamed = flow.load_session(service, sid)
    assert renamed != old_path and '原来源' in summary.header['aliases']
    if route == 'manual':
        assert summary.name == '新的来源'
    if route in {'manual', 'automatic'}:
        assert summary.users[-1]['text'] == text
    flow.write_session(service, summary, renamed)
    assert '原来源' in flow.load_session(service, sid)[0].header['aliases']
    assert path.read_bytes() == before
    assert service.store.classify_project_rules('项目甲')['active'] == [rule]
    assert rule in flow._project_prompt_block(service, '项目甲')
    assert 'aliases' not in handoff.HEADER_FIELDS


def test_source_priority_ambiguity_and_pending_write_rejection(service):
    source(service, '项目乙', '同名', decision='采用旧方案。已失效')
    source(service, '项目甲', '本地改名', aliases=['同名'])
    path = service.store.project_prompt_path('项目甲')
    rule = '- 保留本地规则（来源：[[同名]]#K1）'
    service.store.write_text(path, prompt.render({'硬规则': rule}))
    assert service.store.classify_project_rules('项目甲')['active'] == [rule]
    source(service, '项目甲', '同名', decision='候选方案 | 理由（建议，未确认）')
    assert service.store.classify_project_rules('项目甲')['pending'] == [rule]
    block = flow._project_prompt_block(service, '项目甲')
    sha = service.store.block_fingerprint(rule)[:8]
    assert f'## 硬规则 〔sha:{sha}〕\n\n' in block
    assert sha != service.store.block_fingerprint('')[:8]
    assert service.store.save_project_prompt('项目甲', {'硬规则': rule})['rejected']
    alias_rule = '- 歧义须核对（来源：[[旧别名]]#K1）'
    source(service, '项目甲', '候选一', aliases=['旧别名'])
    source(service, '项目甲', '候选二', aliases=['旧别名'])
    source(service, '项目乙', '旧别名')
    service.store.write_text(path, prompt.render({'硬规则': alias_rule}))
    assert service.store.classify_project_rules('项目甲')['unresolved'] == [alias_rule]
    source(service, '项目乙', '跨项目现名', aliases=['跨项目旧名'])
    assert service.store._prompt_resolver('项目甲')('跨项目旧名', 'K1')
    own, _ = flow.load_session(service, 'codex:项目甲-本地改名')
    own.sections['决策'].append({'number': 2, 'text': '内存中新增的规则'})
    assert service.store._prompt_resolver('项目甲', own)('同名', 'K2') is False
    assert service.store._prompt_resolver('项目甲', own)('本地改名', 'K2') is True


@pytest.mark.parametrize('marker', ['被取代', '已被取代', '已废止', '已废弃', '已作废', '已失效'])
def test_six_explicit_markers_are_inactive_including_sentence_suffix(marker):
    for text in (f'采用A（{marker}）', f'采用A。{marker}', f'采用A；{marker}', f'采用A | {marker}'):
        assert handoff.decision_state(text) == 'inactive'
        assert not handoff.decision_active(text)
    assert handoff.decision_state(f'不要恢复{marker}的方案') == 'active'


def test_aliases_are_deduplicated_and_keep_the_latest_ten(service):
    sid, _ = source(service, '项目甲', '最早名称')
    for number in range(12):
        service.rename_session(sid, f'名称{number}')
    summary, _ = flow.load_session(service, sid)
    assert summary.header['aliases'] == [f'名称{number}' for number in range(1, 11)]
