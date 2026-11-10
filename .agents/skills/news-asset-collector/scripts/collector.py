"""Offline, copy-only media registration. Search/download are agent/browser tasks."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from datetime import datetime, timezone

EXTENSIONS = {
    'image': {'.jpg', '.jpeg', '.png', '.webp'},
    'video': {'.mp4', '.webm', '.mov', '.mkv'},
    'audio': {'.mp3', '.wav', '.m4a', '.ogg', '.flac'},
}


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def token(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f_\s]+', '-', str(value or ''))
    value = value.strip(' .-')[:36] or 'unknown'
    if value.upper().split('.')[0] in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
        value = 'asset-' + value
    return value


def safe(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError('Path escapes project')
    return path


def atomic_json(path, data):
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as out:
        temporary = Path(out.name)
        json.dump(data, out, ensure_ascii=False, indent=2)
        out.write('\n')
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate(record):
    for key in ('id', 'news_id', 'description', 'source', 'provenance', 'confidence', 'topics', 'keywords', 'media_type'):
        if key not in record:
            raise ValueError(f'Missing {key}')
    if not isinstance(record['id'], str) or not record['id']:
        raise ValueError('Nonempty string id required')
    if record['media_type'] not in EXTENSIONS:
        raise ValueError('Unsupported media_type')
    if not isinstance(record['source'], dict) or not str(record['source'].get('page_url', '')).startswith(('https://', 'http://')):
        raise ValueError('HTTP(S) source page_url required')
    for key in ('provenance', 'confidence'):
        if not isinstance(record[key], dict):
            raise ValueError(f'{key} must be an object')
    for key in ('topics', 'keywords'):
        if not isinstance(record[key], list):
            raise ValueError(f'{key} must be a list')
    score = record['confidence'].get('overall')
    if isinstance(score, bool) or not isinstance(score, (float, int)) or not 0 <= score <= 1:
        raise ValueError('confidence.overall must be 0..1')
    if 'licensing' in record and not isinstance(record['licensing'], dict):
        raise ValueError('Optional licensing must be an object')
    if record.get('local_path'):
        source = Path(record['local_path']).resolve()
        if not source.is_file() or source.suffix.lower() not in EXTENSIONS[record['media_type']]:
            raise ValueError('Missing file or mismatched media extension')
    elif not record.get('link_only_reason'):
        raise ValueError('Link-only record requires a reason')


def prepare(root, records, manifest):
    reserved = set()
    result = []
    existing = {a['id']: a for a in manifest['assets']}
    for record in records:
        validate(record)
        if record['id'] in reserved:
            raise ValueError('Duplicate input id')
        reserved.add(record['id'])
        item = dict(record)
        item['file_path'] = None
        if not record.get('local_path'):
            item['status'] = 'link_only'
        else:
            source = Path(record['local_path']).resolve()
            item.update(sha256=digest(source), original_filename=source.name, status='acquired')
            same = next((a for a in manifest['assets'] + result if a.get('sha256') == item['sha256'] and a.get('file_path')), None)
            if same:
                target = safe(root, same['file_path'])
                if (target.is_file() and digest(target) == item['sha256']) or any(a.get('file_path') == same['file_path'] for a in result):
                    item['file_path'] = same['file_path']
                    item['duplicate_of'] = same['id']
            if not item['file_path']:
                stem = '_'.join(token(record.get(k)) for k in ('brand', 'event', 'scene'))
                folder = Path('news') / token(record['news_id']) / record['media_type']
                sequence = 1
                while True:
                    relative = (folder / f'{stem}_{sequence:03d}{source.suffix}').as_posix()
                    target = safe(root, relative)
                    if not target.exists() and relative.casefold() not in {a.get('file_path', '').casefold() for a in result if a.get('file_path')}:
                        break
                    sequence += 1
                item['file_path'] = relative
        previous = existing.get(item['id'])
        if previous:
            # Stable IDs are immutable. Updating evidence requires a new ID.
            comparison = dict(previous)
            for key in ('file_path', 'status', 'sha256', 'original_filename', 'duplicate_of', 'backup_path'):
                comparison.pop(key, None)
            if comparison != record:
                raise ValueError('Existing id has changed metadata; use a new id')
            if previous.get('sha256') != item.get('sha256'):
                raise ValueError('Existing id has changed bytes')
            if previous.get('file_path') and (not safe(root, previous['file_path']).is_file() or digest(safe(root, previous['file_path'])) != previous['sha256']):
                raise ValueError('Registered file missing or modified')
            item = dict(previous)
            item['_skip'] = True
        result.append(item)
    return result


def copy_exclusive(source, target, expected):
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open('xb') as out, source.open('rb') as src:
            shutil.copyfileobj(src, out)
        if digest(target) != expected:
            raise ValueError('Source changed during copy')
    except FileExistsError:
        raise
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def run(project, input_path, apply=False):
    root = Path(project).resolve()
    records = json.loads(Path(input_path).read_text(encoding='utf-8-sig'))
    if not isinstance(records, list):
        raise ValueError('Input must be a JSON list')
    if apply:
        root.mkdir(parents=True, exist_ok=True)
    lock = root / '.news-asset-collector.lock'
    handle = None
    try:
        if apply:
            handle = lock.open('x')
        manifest_path = safe(root, 'assets.json')
        manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig')) if manifest_path.exists() else {'schema_version': 1, 'assets': []}
        if manifest.get('schema_version') != 1 or not isinstance(manifest.get('assets'), list):
            raise ValueError('Unrecognized manifest; refusing overwrite')
        items = prepare(root, records, manifest)
        if not apply:
            return items
        for item in items:
            if item.pop('_skip', False):
                continue
            if item['file_path']:
                src = Path(item['local_path']).resolve()
                backup = safe(root, f"originals/{item['sha256']}/{token(src.name)}{src.suffix}")
                if backup.exists():
                    if digest(backup) != item['sha256']:
                        raise ValueError('Backup hash mismatch')
                else:
                    copy_exclusive(src, backup, item['sha256'])
                item['backup_path'] = backup.relative_to(root).as_posix()
                target = safe(root, item['file_path'])
                if not target.exists():
                    copy_exclusive(src, target, item['sha256'])
                elif digest(target) != item['sha256']:
                    raise ValueError('Destination changed; rerun plan')
            manifest['assets'].append(item)
            manifest['updated_at'] = datetime.now(timezone.utc).isoformat()
            atomic_json(manifest_path, manifest)
        return items
    finally:
        if handle is not None:
            handle.close()
            lock.unlink()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['plan', 'ingest'])
    parser.add_argument('--project', required=True)
    parser.add_argument('--input', required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.project, args.input, args.command == 'ingest'), ensure_ascii=False, indent=2))
