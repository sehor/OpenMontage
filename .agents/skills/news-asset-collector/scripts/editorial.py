"""Validate and export an agent-written report tied to project news and assets."""
import argparse
import hashlib
import json
import math
from pathlib import Path
from datetime import datetime, timezone
import collector


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def narration_digest(plan):
    content = {'title': plan['title'], 'paragraphs': [{'id': p['id'], 'text': p['text']} for p in plan['paragraphs']]}
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def validate(root, plan):
    news = load(root/'news.json')
    require(news.get('selection_status') == 'confirmed', 'News selection is not confirmed')
    selected = set(news['selected_story_ids'])
    assets = {a['id']: a for a in load(root/'assets.json')['assets']}
    require(bool(plan.get('title')) and plan.get('status') == 'draft', 'Title and draft status required')
    paragraphs = plan.get('paragraphs')
    require(isinstance(paragraphs, list) and bool(paragraphs), 'Paragraphs required')
    seen = set()
    for p in paragraphs:
        pid = p.get('id')
        require(isinstance(pid, str) and bool(pid) and pid not in seen, 'Unique paragraph IDs required')
        seen.add(pid)
        require(isinstance(p.get('text'), str) and bool(p['text'].strip()), f'{pid}: narration required')
        ids = p.get('news_ids')
        require(isinstance(ids, list) and set(ids) <= selected, f'{pid}: references unselected news')
        sources = p.get('sources')
        require(isinstance(sources, list) and (not ids or bool(sources)), f'{pid}: factual paragraph needs sources')
        for source in sources:
            require(str(source.get('url', '')).startswith(('http://', 'https://')) and bool(source.get('support')), f'{pid}: source URL and supporting fact required')
        gaps, shots = p.get('gaps'), p.get('shots')
        require(isinstance(gaps, list) and all(isinstance(g, str) and g.strip() for g in gaps), f'{pid}: invalid gaps')
        require(isinstance(shots, list) and (bool(shots) or bool(gaps)), f'{pid}: add shots or explain visual gap')
        for shot in shots:
            asset = assets.get(shot.get('asset_id'))
            require(asset is not None, f'{pid}: missing asset ID')
            require(asset.get('status') == 'acquired' and asset.get('file_path'), f'{pid}: shot is not downloaded')
            file = collector.safe(root, asset['file_path'])
            require(file.is_file(), f'{pid}: missing media file')
            require(bool(asset.get('sha256')) and collector.digest(file) == asset['sha256'], f'{pid}: asset hash changed')
            require(shot.get('role') in ('evidence', 'illustration') and bool(shot.get('note')), f'{pid}: shot role/note required')
            require(shot['role'] != 'evidence' or asset.get('news_id') in ids, f'{pid}: unrelated footage cannot be evidence')
            if asset['media_type'] == 'video':
                start, end = shot.get('in_seconds'), shot.get('out_seconds')
                duration = asset.get('technical', {}).get('duration_seconds')
                nums = (start, end, duration)
                require(all(isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n) for n in nums), f'{pid}: measured duration and finite video timecodes required')
                require(0 <= start < end <= duration, f'{pid}: video timecodes outside source duration')
            else:
                require('in_seconds' not in shot and 'out_seconds' not in shot, f'{pid}: only video shots use source timecodes')
    approval = plan.get('text_approval')
    if approval is not None:
        require(isinstance(approval, dict) and approval.get('status') == 'approved', 'Invalid recorded text approval')
        require(bool(approval.get('user_response')) and bool(approval.get('recorded_at')), 'Actual user response and approval recording time required')
        require(approval.get('approved_text_sha256') == narration_digest(plan), 'Approved narration changed; obtain review for changed text')
    return assets


def render(plan):
    status = '状态：正文已确认，画面关联已更新' if plan.get('text_approval') else '状态：初稿，待审阅'
    lines = [f"# {plan['title']}", '', status, '']
    for p in plan['paragraphs']:
        lines.extend([f"## {p['id']}", '', p['text'], '', '编辑备注（不口播）：', ''])
        lines.extend(f"- 来源：[{s['url']}]({s['url']})；支持：{s['support']}" for s in p['sources'])
        lines.extend(f"- 素材：{s['asset_id']}；{s['role']}；{s['note']}" for s in p['shots'])
        lines.extend(f'- 缺口：{g}' for g in p['gaps'])
        lines.append('')
    return '\n'.join(lines)


def run(project, input_path=None, apply=False, check=False):
    root = Path(project).resolve()
    handle = None
    lock = root/'.news-asset-collector.lock'
    try:
        if apply:
            handle = lock.open('x')
        plan = load(root/'report.json' if check else Path(input_path))
        assets = validate(root, plan)
        markdown = render(plan)
        if check:
            board = load(root/'storyboard.json')
            require(set(board.get('input_hashes', {})) == {'news.json', 'assets.json', 'report.json', 'report.md'}, 'Incomplete freshness hashes')
            for name, expected in board['input_hashes'].items():
                require(collector.digest(collector.safe(root, name)) == expected, f'{name}: changed; regenerate and review')
            require((root/'report.md').read_text(encoding='utf-8') == markdown, 'Report markdown changed; synchronize structured source')
            expected = mapping(plan, assets)
            require(board['paragraphs'] == expected, 'Storyboard mapping changed; regenerate')
            return {'status': 'draft_valid', 'paragraphs': len(plan['paragraphs'])}
        summary = {'status': 'draft_preview', 'paragraphs': len(plan['paragraphs']), 'gaps': sum(len(p['gaps']) for p in plan['paragraphs'])}
        if not apply:
            return summary
        names = ('report.json', 'report.md', 'storyboard.json')
        if any((root/name).exists() for name in names):
            folder = root/'artifacts'/('editorial-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
            folder.mkdir(parents=True)
            for name in names:
                file = collector.safe(root, name)
                if file.exists():
                    (folder/name).write_bytes(file.read_bytes())
        collector.atomic_json(collector.safe(root, 'report.json'), plan)
        # Reuse the JSON atomic writer's staging pattern for Markdown.
        import os
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='', dir=root, delete=False) as out:
            temporary = Path(out.name)
            out.write(markdown)
        try:
            os.replace(temporary, collector.safe(root, 'report.md'))
        finally:
            temporary.unlink(missing_ok=True)
        board = {'schema_version': 1, 'status': 'draft', 'input_hashes': {name: collector.digest(collector.safe(root, name)) for name in ('news.json', 'assets.json', 'report.json', 'report.md')}, 'paragraphs': mapping(plan, assets)}
        collector.atomic_json(collector.safe(root, 'storyboard.json'), board)
        return dict(summary, status='draft_saved')
    finally:
        if handle is not None:
            handle.close()
            lock.unlink()


def mapping(plan, assets):
    return [{'paragraph_id': p['id'], 'news_ids': p['news_ids'], 'shots': [dict(s, file_path=assets[s['asset_id']]['file_path'], sha256=assets[s['asset_id']]['sha256']) for s in p['shots']], 'gaps': p['gaps']} for p in plan['paragraphs']]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--input')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    if (args.check and (args.input or args.apply)) or (not args.check and not args.input):
        parser.error('Use --input [--apply], or --check alone')
    print(json.dumps(run(args.project, args.input, args.apply, args.check), ensure_ascii=False, indent=2))
