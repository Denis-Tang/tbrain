from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import threading

import pytest

from tbrain.store import Store

spec = importlib.util.spec_from_file_location('public_backfill', Path(__file__).parents[1] / 'scripts/dev/public_backfill.py')
backfill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backfill)
STAMP = datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()


def card(vault, name, *, newline=b'\n', bom=b'', diff=0, owner=''):
    path = vault / '公共池' / '配置' / (name + '.md')
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (f'---\nid: 配置-{name}\nkind: 配置\nname: {name}\nstate: 生效\n'
            'updated_at: 2026-11-01T00:00:00Z\n# 原注释保持\n' +
            (f'owner: {owner}\n' if owner else '') + '---\n\nPRIVATE_BODY 合成正文\n')
    path.write_bytes(bom + text.encode('utf-8').replace(b'\n', newline))
    os.utime(path, (STAMP + diff, STAMP + diff))
    return path


def selection(report, *names, add_owner=False):
    return {'selected': [{**row, 'add_owner': add_owner} for row in report['cards'] if row['name'] in names]}


@pytest.mark.parametrize('newline,bom', [(b'\n', b''), (b'\r\n', b''), (b'\n', b'\xef\xbb\xbf'), (b'\r\n', b'\xef\xbb\xbf')])
def test_preview_selection_bytes_backup_and_overwrite_risk(tmp_path, monkeypatch, newline, bom):
    vault = tmp_path / 'vault'
    original = card(vault, '原样', newline=newline, bom=bom)
    edited = card(vault, '手改', newline=newline, bom=bom, diff=600)
    user = card(vault, '用户所有', newline=newline, bom=bom, owner='user')
    for relative in ('公共池/配置/归档/旧.md', '公共池/冲突/冲突.md', '公共池/待用户确认.md'):
        path = vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'KEEP')
    marker = vault / '.tbrain' / 'write.lock'
    marker.parent.mkdir()
    marker.write_bytes(b'K')
    before = backfill.tree_hashes(vault)

    def forbidden(*args, **kwargs):
        raise AssertionError('preview must not construct Store')

    with monkeypatch.context() as patch:
        patch.setattr(backfill, 'Store', forbidden)
        default = backfill.preview(vault)
        report = backfill.preview(vault, theta=30, mtime_usable=True, theta_source='合成测试输入')
    assert default['counts'] == {'A': 0, 'B': 0, 'C': 0, 'D': 3}
    assert report['counts'] == {'A': 0, 'B': 0, 'C': 1, 'D': 2}
    assert report['selected'] == [] and 'PRIVATE_BODY' not in json.dumps(report)
    assert report['mtime_hour_buckets'] and report['mtime_peak_minute']['total'] == 3
    assert backfill.tree_hashes(vault) == before
    approved = selection(report, '原样', add_owner=newline == b'\r\n')
    original_bytes, edited_bytes, user_bytes = original.read_bytes(), edited.read_bytes(), user.read_bytes()
    result = backfill.apply(vault, approved, tmp_path / 'backups', tmp_path / 'lock-home')
    assert result['written'] == 1 and result['skipped'] == 0
    manifest = json.loads(Path(result['manifest']).read_text(encoding='utf-8'))
    backup = Path(manifest['backup']) / '公共池'
    assert backfill.tree_hashes(backup) == {key.removeprefix('公共池/'): value for key, value in before.items() if key.startswith('公共池/')}
    entry = manifest['entries'][0]
    raw, added = original.read_bytes(), entry['inserted'].encode('utf-8')
    assert raw[:entry['offset']] + raw[entry['offset'] + len(added):] == original_bytes
    data, body, _, _ = backfill.parse_card(raw)
    assert data['model_fingerprint'] == backfill.fingerprint(data, body)
    assert ('owner' in data) == (newline == b'\r\n')
    assert edited.read_bytes() == edited_bytes and user.read_bytes() == user_bytes
    assert marker.read_bytes() == b'K'
    after = backfill.tree_hashes(vault)
    repeated = backfill.apply(vault, approved, tmp_path / 'backups', tmp_path / 'lock-home')
    assert repeated['written'] == 0 and repeated['results'][0]['reason'] == '已有有效指纹'
    assert backfill.tree_hashes(vault) == after

    store = Store(vault, tmp_path / 'normal-home')
    with pytest.raises(ValueError):
        store.save_public_card('配置', '手改', {'body': '替代正文'})
    assert '待用户确认' in (vault / '公共池' / '待用户确认.md').read_text(encoding='utf-8')
    forced = backfill.apply(vault, selection(report, '手改'), tmp_path / 'backups', tmp_path / 'lock-home')
    assert forced['written'] == 1
    replaced = store.save_public_card('配置', '手改', {'body': '替代正文'})
    assert replaced['replaced'] and Path(replaced['archived_path']).is_file()
    assert '替代正文' in Path(replaced['path']).read_text(encoding='utf-8')


def test_rollback_boundaries_hash_change_and_twenty_interleavings(tmp_path, monkeypatch):
    vault = tmp_path / 'vault'
    marker = vault / '.tbrain' / 'write.lock'
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b'K')
    store = Store(vault, tmp_path / 'normal-home')
    with ThreadPoolExecutor(max_workers=2) as executor:
        for number in range(20):
            name = f'交错{number}'
            path = card(vault, name)
            report = backfill.preview(vault, theta=30, mtime_usable=True)
            approved = selection(report, name)
            barrier = threading.Barrier(2)

            def fill():
                barrier.wait()
                return backfill.apply(vault, approved, tmp_path / 'backups', tmp_path / 'lock-home')

            def replace():
                barrier.wait()
                try:
                    store.save_public_card('配置', name, {'body': '并发新内容'})
                    return True
                except ValueError:
                    return False

            filling, replacing = executor.submit(fill), executor.submit(replace)
            filling.result()
            accepted = replacing.result()
            data, body, _, _ = backfill.parse_card(path.read_bytes())
            assert data['model_fingerprint'] == backfill.fingerprint(data, body)
            assert ('并发新内容' in body) if accepted else ('PRIVATE_BODY' in body)
    assert marker.read_bytes() == b'K'

    paths = [card(vault, f'回滚{number}', owner='legacy' if number == 4 else '') for number in range(6)]
    before = {path: path.read_bytes() for path in paths}
    report = backfill.preview(vault, theta=30, mtime_usable=True)
    approved = selection(report, *(path.stem for path in paths), add_owner=True)
    paths[-1].write_bytes(paths[-1].read_bytes() + b'CHANGED_AFTER_PREVIEW')
    changed_preview = paths[-1].read_bytes()
    result = backfill.apply(vault, approved, tmp_path / 'backups', tmp_path / 'lock-home')
    assert result['written'] == 5 and result['skipped'] == 1
    assert paths[-1].read_bytes() == changed_preview
    assert backfill.parse_card(paths[4].read_bytes())[0]['owner'] == 'legacy'
    for path in paths[:3]:
        path.write_bytes(path.read_bytes() + b'USER_LATER_EDIT')
    later = {path: path.read_bytes() for path in paths[:3]}
    categories = backfill.preview(vault)['counts']
    assert categories['A'] >= 20 and categories['B'] == 3
    manifest = json.loads(Path(result['manifest']).read_text(encoding='utf-8'))
    rolled = backfill.rollback(vault, manifest, tmp_path / 'lock-home')
    assert (rolled['restored'], rolled['skipped'], rolled['failed']) == (2, 3, 0)
    assert all(path.read_bytes() == raw for path, raw in later.items())
    assert all(path.read_bytes() == before[path] for path in paths[3:5])
    assert marker.read_bytes() == b'K'
    archive = vault / '公共池' / '配置' / '归档' / 'symlink-target.md'
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(b'ARCHIVE_SENTINEL')
    os.symlink(archive, vault / '公共池' / '配置' / 'link.md')
    current = backfill.tree_hashes(vault)
    with pytest.raises(ValueError, match='正式'):
        backfill.apply(vault, {'selected': [{'path': '公共池/配置/link.md', 'hash': backfill.digest(archive.read_bytes()),
                                           'category': 'D'}]}, tmp_path / 'backups', tmp_path / 'lock-home')
    assert archive.read_bytes() == b'ARCHIVE_SENTINEL'
    assert backfill.apply(vault, {}, tmp_path / 'backups', tmp_path / 'lock-home')['written'] == 0
    with pytest.raises(ValueError, match='Vault外'):
        backfill.apply(vault, approved, vault / 'backup', tmp_path / 'lock-home')
    for relative in ('../x.md', '公共池/冲突/x.md', '公共池/配置/归档/x.md', '公共池/待用户确认.md'):
        with pytest.raises(ValueError):
            backfill.apply(vault, {'selected': [{'path': relative, 'hash': 'x', 'category': 'D'}]}, tmp_path / 'backups', tmp_path / 'lock-home')
    with pytest.raises(ValueError):
        backfill.apply(vault, {'selected': [{'path': '公共池/配置/回滚3.md', 'hash': 'x', 'category': 'B'}]}, tmp_path / 'backups', tmp_path / 'lock-home')
    with pytest.raises(ValueError, match='不一致'):
        backfill.rollback(tmp_path / 'other-vault', manifest, tmp_path / 'lock-home')

    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2030, 1, 1, tzinfo=timezone.utc)

    monkeypatch.setattr(backfill, 'datetime', Fixed)
    collision = tmp_path / 'existing-backups' / 'r4b-backup-20300101-000000-000000'
    collision.mkdir(parents=True)
    with pytest.raises(FileExistsError):
        backfill.apply(vault, approved, collision.parent, tmp_path / 'lock-home')
    assert collision.is_dir() and backfill.tree_hashes(vault) == current


def test_failed_manifest_or_card_replace_keeps_bytes_and_cleans_own_temp(tmp_path, monkeypatch):
    vault = tmp_path / "vault ' $DOLLAR"
    path = card(vault, '失败注入')
    marker = vault / '.tbrain' / 'write.lock'
    marker.parent.mkdir()
    marker.write_bytes(b'K')
    approved = selection(backfill.preview(vault, theta=30, mtime_usable=True), '失败注入')
    before = backfill.tree_hashes(vault)
    replace = os.replace
    for failure in ('manifest', 'card'):
        backups = tmp_path / ("backups ' $DOLLAR-" + failure)

        def fail(source, destination):
            destination = Path(destination)
            if failure == 'manifest' and destination.name.endswith('-manifest.json'):
                if json.loads(Path(source).read_text(encoding='utf-8'))['entries']:
                    raise OSError('manifest replace failed')
            if failure == 'card' and destination == path:
                saved = json.loads(next(backups.glob('*-manifest.json')).read_text(encoding='utf-8'))
                assert saved['entries'][0]['before_hash'] == backfill.digest(path.read_bytes())
                raise OSError('card replace failed')
            return replace(source, destination)

        with monkeypatch.context() as patch:
            patch.setattr(backfill.os, 'replace', fail)
            with pytest.raises(OSError, match=failure + ' replace failed'):
                backfill.apply(vault, approved, backups, tmp_path / 'lock-home')
        saved = json.loads(next(backups.glob('*-manifest.json')).read_text(encoding='utf-8'))
        assert len(saved['entries']) == (1 if failure == 'card' else 0)
        assert backfill.tree_hashes(vault) == before
        assert not list(vault.rglob('.r4b-*.tmp')) and not list(backups.rglob('.r4b-*.tmp'))
        readme = next(backups.glob('r4b-backup-*/README.txt')).read_text(encoding='utf-8')
        assert "--vault '" + str(vault).replace("'", "''") + "'" in readme
        assert "--manifest '" + str(next(backups.glob('*-manifest.json'))).replace("'", "''") + "'" in readme
