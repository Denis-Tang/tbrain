"""Project identity and attachment ownership, using temporary vaults only."""

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re

import pytest

from tbrain.store import Store, render_note, split_front_matter


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'vault', tmp_path / 'home',
                  now_fn=lambda: datetime(2026, 11, 12, tzinfo=timezone.utc))
    value.ensure_layout()
    return value


def file_hashes(folder):
    return {path.relative_to(folder).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in folder.rglob('*') if path.is_file()}


def test_new_identity_and_narrow_switch_survive_edit_and_rename(store):
    project = store.save_project('项目甲', [])
    project_id = project['project_id']
    assert re.fullmatch(r'p-[0-9a-f]{12}', project_id)
    assert project['read_standalone'] is False
    path = store.project_card_path('项目甲')
    before, body = split_front_matter(store.read_text(path))
    enabled = store.set_project_read_standalone('项目甲', True)
    after, _ = split_front_matter(store.read_text(path))
    assert enabled['read_standalone'] is True
    assert after == {**before, 'read_standalone': True}
    store.save_project('项目甲', [], allow_child=True)
    renamed = store.save_project('新项目甲', [], rename_from='项目甲')
    assert renamed['project_id'] == project_id and renamed['read_standalone'] is True
    assert split_front_matter(store.read_text(store.project_card_path('新项目甲')))[1] == body


def test_project_filename_collision_does_not_alias_or_overwrite_another_project(store):
    project = store.save_project('A  B', [r'D:\original'])
    store.save_project('其他项目', [r'D:\other'])
    before = file_hashes(store.vault)

    assert store.project('A B') is None
    with pytest.raises(ValueError, match='项目目录已属于其他项目'):
        store.save_project('A B', [r'D:\replacement'])
    with pytest.raises(ValueError, match='项目目录已属于其他项目'):
        store.save_project('A B', [], rename_from='其他项目')
    with pytest.raises(ValueError, match='项目不存在'):
        store.save_project('新项目', [], rename_from='A B')

    assert file_hashes(store.vault) == before
    assert store.project('A  B')['project_id'] == project['project_id']
    assert store.project('A  B')['paths'] == [r'D:\original']


def test_legacy_reads_and_edits_do_not_backfill_identity(store):
    path = store.project_card_path('旧项目')
    path.parent.mkdir(parents=True)
    path.write_bytes('---\nname: 旧项目\npaths: []\nx_unknown: keep\n---\nbody\n'.encode('utf-8'))
    before = path.read_bytes()
    assert store.project('旧项目')['project_id'] is None
    assert store.project('旧项目')['read_standalone'] is False
    store.projects()
    assert path.read_bytes() == before
    store.save_project('旧项目', [], allow_child=True)
    data, _ = split_front_matter(store.read_text(path))
    assert 'project_id' not in data and 'read_standalone' not in data
    with pytest.raises(ValueError):
        store.set_project_read_standalone('旧项目', 'on')
    with pytest.raises(ValueError, match='不存在'):
        store.set_project_read_standalone('不存在', True)


@pytest.mark.parametrize('reserved', ['__standalone__', ' __standalone__ '])
def test_reserved_name_rejection_does_not_change_existing_files(store, reserved):
    empty_before = file_hashes(store.vault)
    with pytest.raises(ValueError, match='保留名 __standalone__'):
        store.save_project(reserved, [])
    assert file_hashes(store.vault) == empty_before
    store.save_project('项目甲', [])
    before = file_hashes(store.vault)
    with pytest.raises(ValueError, match='保留名 __standalone__'):
        store.save_project(reserved, [])
    with pytest.raises(ValueError, match='保留名 __standalone__'):
        store.save_project(reserved, [], rename_from='项目甲')
    assert file_hashes(store.vault) == before
    assert store.save_project('__STANDALONE__', [])['name'] == '__STANDALONE__'


def test_rename_follows_session_headers_and_updates_attachment_names(store):
    project_id = store.save_project('旧项目', [])['project_id']
    store.save_project('其他项目', [])
    sessions = store.vault / '会话总结'
    ordinary = sessions / '旧项目' / '2026' / '11' / '普通.md'
    misplaced = sessions / '独立' / '2026' / '11' / '错位.md'
    unrelated = sessions / '旧项目' / '2026' / '11' / '其他.md'
    for path, project in ((ordinary, '旧项目'), (misplaced, '旧项目'), (unrelated, '其他项目')):
        store.write_text(path, render_note({'name': path.stem, 'project': project}, '正文'))
    attachments = store.vault / '附件' / '资料'
    legacy = attachments / '按名.md'
    stable = attachments / '按ID.md'
    global_card = attachments / '通用.md'
    store.write_text(legacy, render_note({'name': '按名', 'projects': ['旧项目']}, '旧名附件'))
    store.write_text(stable, render_note({'name': '按ID', 'projects': [project_id]}, '稳定附件'))
    store.write_text(global_card, render_note({'name': '通用', 'projects': []}, '全局附件'))
    stable_before = stable.read_bytes()
    store.save_project('新项目', [], rename_from='旧项目')
    assert split_front_matter(store.read_text(misplaced))[0]['project'] == '新项目'
    moved = sessions / '新项目' / '2026' / '11'
    assert split_front_matter(store.read_text(moved / '普通.md'))[0]['project'] == '新项目'
    assert split_front_matter(store.read_text(moved / '其他.md'))[0]['project'] == '其他项目'
    assert split_front_matter(store.read_text(legacy))[0]['projects'] == ['新项目']
    assert stable.read_bytes() == stable_before
    assert {card['name'] for card in store.attachments('新项目')} == {'按名', '按ID', '通用'}
    assert {card['name'] for card in store.attachments(project_id)} == {'按名', '按ID', '通用'}
    assert {card['name'] for card in store.attachments()} == {'通用'}


@pytest.mark.parametrize('recovery_file', ['地图.md', '决策日志.md'])
def test_id_preview_is_readonly_and_apply_inserts_only_one_line(store, tmp_path, recovery_file):
    recovered_id = 'p-0123456789ab'
    existing = store.save_project('已有ID', [])['project_id']
    raw_cards = {}
    for name, newline, bom in [('需恢复', b'\r\n', b'\xef\xbb\xbf'), ('需分配', b'\n', b'')]:
        path = store.project_card_path(name)
        path.parent.mkdir(parents=True)
        raw = bom + newline.join([b'---', f'name: {name}'.encode(), b'paths: []',
                                  b'# keep comment', b'x_unknown: 42', b'---', b'', b'user body', b''])
        path.write_bytes(raw)
        raw_cards[name] = raw
    store.write_text(store.project_dir('需恢复') / recovery_file,
                     render_note({'project_id': recovered_id}, '既有产物'))
    before = file_hashes(store.vault)
    preview = store.project_ids()
    assert file_hashes(store.vault) == before
    assignments = {item['name']: item['project_id'] for item in preview['assignments']}
    assert assignments['需恢复'] == recovered_id
    assert re.fullmatch(r'p-[0-9a-f]{12}', assignments['需分配'])
    assert '已有ID' not in assignments
    backup_dir = tmp_path / 'backup'
    applied = store.project_ids(apply=True, backup_dir=str(backup_dir), assignments=preview['assignments'])
    assert applied['updated'] == 2
    for name, raw in raw_cards.items():
        actual = store.project_card_path(name).read_bytes()
        newline = b'\r\n' if b'\r\n' in raw else b'\n'
        added = f'project_id: {assignments[name]}'.encode() + newline
        assert actual.count(added) == 1
        assert actual.replace(added, b'', 1) == raw
        assert (Path(applied['backup']) / name / '项目卡.md').read_bytes() == raw
    assert store.project('已有ID')['project_id'] == existing
    assert store.project_ids()['assignments'] == []


@pytest.mark.parametrize('conflict', ['within_assignments', 'existing_project'])
def test_id_apply_rejects_external_id_collisions_before_writing(store, tmp_path, conflict):
    existing = store.save_project('已有ID', [])['project_id']
    for name in ('旧卡甲', '旧卡乙'):
        store.write_text(store.project_card_path(name), render_note({'name': name, 'paths': []}))
    value = existing if conflict == 'existing_project' else 'p-123456789abc'
    assignments = [{'name': '旧卡甲', 'project_id': value}]
    if conflict == 'within_assignments':
        assignments.append({'name': '旧卡乙', 'project_id': value})
    before = file_hashes(store.vault)
    backup = tmp_path / 'backup'
    with pytest.raises(ValueError, match='project_id'):
        store.project_ids(apply=True, backup_dir=str(backup), assignments=assignments)
    assert file_hashes(store.vault) == before and not backup.exists()
