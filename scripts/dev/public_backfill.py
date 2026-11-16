"""Explicit public-card fingerprint backfill; preview never constructs a Store."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid

import yaml

from tbrain.store import PUBLIC_KINDS, Store, block_fingerprint, render_note

RISK = ('- **必须显式处理的风险：回填会把"当前内容"当成"模型写入内容"登记**。后果是：此后任何 Agent 取代这张卡时，'
        '`store.py:1058` 的校验会通过（指纹相等），旧卡被静默改成 `已被取代` 并移入归档——'
        '**此前用户手改过的内容在启动包与检索里消失（只剩归档副本），而不是像现在这样进待用户确认并报错**。'
        '也就是说，回填对"手改过的缺指纹卡"是把安全失败变成静默覆盖。所以：'
        '\nmtime只是线索，不是证明，可能漏判或误判；C类也需要用户确认。')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def parse_card(raw):
    opening = re.match(rb'(?:\xef\xbb\xbf)?---[ \t]*(\r?\n)', raw)
    if not opening:
        raise ValueError('缺少YAML文件头')
    closing = re.search(rb'(?m)^---[ \t]*(?:\r?\n|$)', raw[opening.end():])
    if closing is None:
        raise ValueError('缺少YAML结束行')
    offset = opening.end() + closing.start()
    newline = opening[1]
    remainder = raw.replace(b'\r\n', b'')
    if b'\r' in remainder or (newline == b'\n' and b'\r' in raw) or (newline == b'\r\n' and b'\n' in remainder):
        raise ValueError('换行不是一致的LF或CRLF')
    text = raw[opening.end():offset].decode('utf-8')
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError('YAML文件头不是映射')
    body = raw[opening.end() + closing.end():].decode('utf-8').replace('\r\n', '\n')
    return data, body, offset, newline


def fingerprint(data, body):
    return block_fingerprint(render_note({key: value for key, value in data.items() if key != 'model_fingerprint'}, body))


def preview(vault, *, theta=None, mtime_usable=False, theta_source='未复核'):
    vault = Path(vault)
    cards, hours, minutes = [], Counter(), Counter()
    for kind in PUBLIC_KINDS:
        for path in sorted((vault / '公共池' / kind).glob('*.md')):
            raw = path.read_bytes()
            mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            hours[mtime.strftime('%Y-%m-%dT%H:00Z')] += 1
            minutes[mtime.strftime('%Y-%m-%dT%H:%MZ')] += 1
            row = dict(path=path.relative_to(vault).as_posix(), hash=digest(raw), kind=kind, name=path.stem,
                       category='D', updated_at='', mtime_utc=mtime.isoformat(), diff_seconds=None,
                       mtime_triggered=None, owner=None, body_chars=0, reason='')
            try:
                data, body, _, _ = parse_card(raw)
                row.update(name=str(data.get('name') or path.stem), updated_at=str(data.get('updated_at') or ''),
                           owner=data.get('owner'), body_chars=len(body))
                date_error = ''
                try:
                    updated = datetime.fromisoformat(row['updated_at'])
                    if updated.tzinfo is None:
                        raise ValueError('updated_at缺少时区')
                    row['diff_seconds'] = round(mtime.timestamp() - updated.timestamp(), 6)
                except ValueError:
                    date_error = 'updated_at缺失、无法解析或缺少时区'
                if 'model_fingerprint' in data:
                    row['category'] = 'A' if data['model_fingerprint'] == fingerprint(data, body) else 'B'
                    row['reason'] = '已有有效指纹' if row['category'] == 'A' else '已有指纹不符，不回填'
                else:
                    row['mtime_triggered'] = row['diff_seconds'] > theta if not date_error and theta is not None and mtime_usable else None
                    if date_error:
                        row['reason'] = date_error
                    elif data.get('owner') == 'user':
                        row['reason'] = 'owner为user'
                    elif theta is None or not mtime_usable:
                        row['reason'] = '本批mtime不可用或阈值未复核'
                    elif row['mtime_triggered']:
                        row['reason'] = 'mtime差超过本批阈值'
                    else:
                        row.update(category='C', reason='缺指纹且本批mtime线索未触发，仍需用户选择')
            except (ValueError, UnicodeError, yaml.YAMLError) as exc:
                row['reason'] = str(exc).splitlines()[0]
            cards.append(row)
    peak = max(minutes.items(), key=lambda item: item[1]) if minutes else ('', 0)
    return dict(vault=str(vault.resolve()), cards=cards, counts={kind: sum(row['category'] == kind for row in cards) for kind in 'ABCD'},
                theta=theta, theta_source=theta_source, mtime_usable=mtime_usable,
                mtime_hour_buckets=dict(sorted(hours.items())),
                mtime_peak_minute=dict(minute=peak[0], count=peak[1], total=len(cards)),
                risk=RISK, selected=[], approval='', reason_report='')


def write_preview(report, out):
    out = Path(out).resolve()
    allowed = Path(tempfile.gettempdir()).resolve() / 'r4b-preview'
    if not out.is_relative_to(allowed):
        raise ValueError('预览输出须在TEMP/r4b-preview下')
    if out.is_relative_to(Path(report['vault'])):
        raise ValueError('预览输出不得写入输入Vault')
    out.mkdir(parents=True)
    (out / 'preview.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# 公共池指纹只读预览', '', json.dumps({key: value for key, value in report.items() if key not in
             ('cards', 'selected', 'approval', 'reason_report', 'risk')}, ensure_ascii=False, indent=2), '', RISK, '',
             '| 类别 | 相对路径 | 名称 | updated_at | mtime UTC | 差秒 | 触发 | owner | 正文字数 | 当前SHA256 | 原因 |',
             '|---|---|---|---|---|---:|---|---|---:|---|---|']
    for row in report['cards']:
        values = [row[key] for key in ('category', 'path', 'name', 'updated_at', 'mtime_utc', 'diff_seconds',
                                      'mtime_triggered', 'owner', 'body_chars', 'hash', 'reason')]
        lines.append('| ' + ' | '.join(str(value).replace('|', '\\|').replace('\n', ' ') for value in values) + ' |')
    (out / 'preview.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return out


def card_path(vault, relative):
    path = Path(vault).resolve().joinpath(relative).resolve()
    if not path.is_relative_to(Path(vault).resolve()):
        raise ValueError('卡路径超出Vault')
    parts = path.relative_to(Path(vault).resolve()).parts
    if len(parts) != 3 or parts[0] != '公共池' or parts[1] not in PUBLIC_KINDS or Path(parts[2]).suffix != '.md':
        raise ValueError('只处理公共池七种正式目录的直接md卡')
    return path


def tree_hashes(folder):
    return {path.relative_to(folder).as_posix(): digest(path.read_bytes()) for path in Path(folder).rglob('*') if path.is_file()}


def write_bytes(path, raw):
    temporary = path.with_name('.r4b-' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_bytes(raw)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def lock(vault, app_home):
    app_home = Path(app_home).resolve()
    if not app_home.is_relative_to(Path(tempfile.gettempdir()).resolve()) or (app_home.exists() and any(app_home.iterdir())):
        raise ValueError('app_home须是TEMP下的空目录或新路径')
    return Store(Path(vault), app_home).write_lock()


def apply(vault, approved, backup_dir, app_home):
    vault, backup_dir = Path(vault).resolve(), Path(backup_dir).resolve()
    if backup_dir.is_relative_to(vault):
        raise ValueError('备份目录须在Vault外')
    selected = approved.get('selected') or []
    for row in selected:
        card_path(vault, row['path'])
        if row['category'] not in ('C', 'D'):
            raise ValueError('只可显式选择C或D；A不动、B不回填')
    if not selected:
        return {'written': 0, 'skipped': 0, 'results': [], 'manifest': ''}
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    backup = backup_dir / ('r4b-backup-' + stamp)
    manifest_path = backup_dir / ('r4b-backup-' + stamp + '-manifest.json')
    manifest = dict(vault=str(vault), backup=str(backup), approval=approved.get('approval', ''),
                    reason_report=approved.get('reason_report', ''), entries=[])
    results = []
    with lock(vault, app_home):
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup.mkdir()
        source = vault / '公共池'
        shutil.copytree(source, backup / '公共池')
        if tree_hashes(source) != tree_hashes(backup / '公共池'):
            raise ValueError('公共池备份文件数或SHA256不符')
        (backup_dir / ('r4b-backup-' + stamp + '-approved.json')).write_text(
            json.dumps(approved, ensure_ascii=False, indent=2), encoding='utf-8')
        quoted_vault = "'" + str(vault).replace("'", "''") + "'"
        quoted_manifest = "'" + str(manifest_path).replace("'", "''") + "'"
        (backup / 'README.txt').write_text(
            '公共池完整备份：逐文件SHA256和文件数已核对相同。\n正常回滚：\n'
            f'python scripts/dev/public_backfill.py rollback --vault {quoted_vault} --manifest {quoted_manifest} --app-home \'<TEMP新路径>\'\n'
            '正常回滚只撤本次字段，后续修改的卡会跳过。\n'
            '灾难恢复不由本工具执行：须用户明确选择并先关闭所有Tbrain宿主停写，再人工从公共池备份复制；'
            '整目录覆盖会丢失回填后的更新，不可用作正常回滚。\n', encoding='utf-8')
        write_bytes(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8'))
        for row in selected:
            path = card_path(vault, row['path'])
            if not path.is_file():
                results.append(dict(path=row['path'], result='跳过', reason='文件不存在'))
                continue
            raw = path.read_bytes()
            result = dict(path=row['path'], result='跳过')
            try:
                data, body, offset, newline = parse_card(raw)
            except (ValueError, UnicodeError, yaml.YAMLError):
                result['reason'] = '文件头无法解析或换行不支持'
            else:
                if 'model_fingerprint' in data:
                    result['reason'] = '已有有效指纹' if data['model_fingerprint'] == fingerprint(data, body) else '已有指纹不符，不回填'
                elif digest(raw) != row['hash']:
                    result['reason'] = '哈希不符'
                else:
                    inserted = []
                    if row.get('add_owner') is True and 'owner' not in data:
                        data['owner'] = 'model'
                        inserted.append('owner: model')
                    inserted.append(yaml.safe_dump({'model_fingerprint': fingerprint(data, body)}).strip())
                    addition = newline.join(line.encode('utf-8') for line in inserted) + newline
                    changed = raw[:offset] + addition + raw[offset:]
                    parsed, current_body, _, _ = parse_card(changed)
                    assert parsed['model_fingerprint'] == fingerprint(parsed, current_body)
                    assert changed[:offset] + changed[offset + len(addition):] == raw
                    manifest['entries'].append(dict(path=row['path'], before_hash=digest(raw), after_hash=digest(changed),
                                                    offset=offset, inserted=addition.decode('utf-8')))
                    write_bytes(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8'))
                    write_bytes(path, changed)
                    result['result'] = '已写'
            results.append(result)
    return dict(written=sum(row['result'] == '已写' for row in results), skipped=sum(row['result'] == '跳过' for row in results),
                results=results, manifest=str(manifest_path))


def rollback(vault, manifest, app_home):
    vault = Path(vault).resolve()
    if Path(manifest['vault']).resolve() != vault:
        raise ValueError('清单与目标Vault不一致')
    results = []
    for row in manifest['entries']:
        card_path(vault, row['path'])
    with lock(vault, app_home):
        for row in manifest['entries']:
            path = card_path(vault, row['path'])
            if not path.is_file():
                results.append(dict(path=row['path'], result='跳过', reason='文件不存在'))
                continue
            raw = path.read_bytes()
            result = dict(path=row['path'], result='跳过')
            if digest(raw) != row['after_hash']:
                result['reason'] = '回填后有修改'
            else:
                offset, addition = row['offset'], row['inserted'].encode('utf-8')
                restored = raw[:offset] + raw[offset + len(addition):]
                if raw[offset:offset + len(addition)] != addition or digest(restored) != row['before_hash']:
                    result['result'] = '校验失败'
                else:
                    write_bytes(path, restored)
                    result['result'] = '已恢复'
            results.append(result)
    return dict(restored=sum(row['result'] == '已恢复' for row in results), skipped=sum(row['result'] == '跳过' for row in results),
                failed=sum(row['result'] == '校验失败' for row in results), results=results)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    show = commands.add_parser('preview')
    show.add_argument('--vault', type=Path, required=True)
    show.add_argument('--out', type=Path)
    show.add_argument('--theta', type=float)
    show.add_argument('--mtime-usable', action='store_true')
    show.add_argument('--theta-source', default='未复核')
    for action, source in (('apply', 'approved'), ('rollback', 'manifest')):
        sub = commands.add_parser(action)
        sub.add_argument('--vault', type=Path, required=True)
        sub.add_argument('--' + source, type=Path, required=True)
        sub.add_argument('--app-home', type=Path, required=True)
        if action == 'apply':
            sub.add_argument('--backup-dir', type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'preview':
        report = preview(args.vault, theta=args.theta, mtime_usable=args.mtime_usable, theta_source=args.theta_source)
        out = args.out or Path(tempfile.gettempdir()) / 'r4b-preview' / datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
        write_preview(report, out)
        result = dict(counts=report['counts'], preview=str(out), selected=0)
    elif args.action == 'apply':
        result = apply(args.vault, json.loads(args.approved.read_text(encoding='utf-8-sig')), args.backup_dir, args.app_home)
    else:
        result = rollback(args.vault, json.loads(args.manifest.read_text(encoding='utf-8-sig')), args.app_home)
    print(json.dumps(result, ensure_ascii=False, indent=2))
