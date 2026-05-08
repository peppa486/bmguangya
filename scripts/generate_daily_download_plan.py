#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path('/opt/anime-stack')
CONFIG = ROOT / 'config/bangumi-sync.json'
PYTHON = '/opt/guangya-webdav/venv/bin/python'
SYNC = ROOT / 'scripts/bangumi_cloud_download.py'
STATE_DIR = ROOT / 'state'
PLAN_JSON = STATE_DIR / 'pending-download-plan.json'
PLAN_MD = STATE_DIR / 'pending-download-plan.md'
COLLECTION_LABEL_CN = {'doing': '在看', 'wish': '想看', 'collect': '看过'}


def extract_json(text: str) -> dict:
    for m in list(re.finditer(r'\n\{', text)) + ([] if not text.lstrip().startswith('{') else [re.match(r'\{', text.lstrip())]):
        idx = m.start() + (1 if text[m.start()] == '\n' else 0)
        try:
            return json.loads(text[idx:])
        except Exception:
            pass
    raise RuntimeError('cannot parse sync JSON summary')


def main() -> int:
    cfg = json.loads(CONFIG.read_text(encoding='utf-8'))
    cfg['dry_run'] = True
    cfg['max_download_subjects_per_run'] = int(cfg.get('daily_plan_subject_limit') or 3)
    cfg['max_new_cloud_tasks_per_run'] = int(cfg.get('daily_plan_task_limit') or 200)
    cfg['max_episodes_per_subject_per_run'] = int(cfg.get('daily_plan_episodes_per_subject') or 1)
    cfg['episode_selection_mode'] = cfg.get('episode_selection_mode') or 'latest'
    cfg.pop('download_subject_ids', None)

    with tempfile.NamedTemporaryFile('w', encoding='utf-8', suffix='.json', delete=False) as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        tmp_config = f.name

    proc = subprocess.run(
        [PYTHON, str(SYNC), '--config', tmp_config],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=900,
    )
    output = proc.stdout or ''
    if proc.returncode != 0:
        raise SystemExit(output[-4000:])
    summary = extract_json(output)

    grouped = defaultdict(list)
    for item in summary.get('submitted') or []:
        item['collection'] = COLLECTION_LABEL_CN.get(str(item.get('collection')), item.get('collection'))
        grouped[str(item.get('subject_id'))].append(item)

    subjects = []
    for sub in summary.get('downloaded_subjects') or []:
        sid = str(sub.get('subject_id'))
        items = grouped.get(sid, [])
        subjects.append({
            'subject_id': sid,
            'show': sub.get('show'),
            'collection': COLLECTION_LABEL_CN.get(str(sub.get('collection')), sub.get('collection')),
            'category_dir': sub.get('category_dir'),
            'show_dir': sub.get('show_dir'),
            'episode_count': len(items),
            'items': [{
                'title': x.get('title'),
                'target': f"{x.get('target_dir')}/{x.get('target_name')}",
                'target_name': x.get('target_name'),
                'torrent_url': x.get('torrent_url'),
            } for x in items],
        })

    plan = {
        'created_at': datetime.now().astimezone().isoformat(),
        'status': 'pending_confirmation',
        'policy': {
            'subject_limit': cfg['max_download_subjects_per_run'],
            'collection_order': cfg.get('download_collection_types', [3, 1, 2]),
            'order_label': '在看 → 想看 → 看过',
            'requires_confirmation': True,
            'episodes_per_subject': cfg.get('max_episodes_per_subject_per_run', 1),
            'episode_selection_mode': cfg.get('episode_selection_mode', 'latest'),
        },
        'subject_ids': [x['subject_id'] for x in subjects],
        'subjects': subjects,
        'summary': {k: summary.get(k) for k in [
            'submitted_count', 'unresolved_count', 'mismatched_count',
            'skipped_existing_target_count', 'dry_run', 'dedup_stats'
        ]},
    }
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PLAN_JSON.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')

    lines = []
    lines.append('## 明日光鸭下载候选计划（待确认）')
    lines.append('')
    lines.append(f"生成时间：{plan['created_at']}")
    lines.append('策略：每天最多 3 部番；每部默认只取最新 1 集；顺序：在看 → 想看 → 看过。确认前不会真实下载。')
    lines.append('')
    if not subjects:
        lines.append('没有找到可加入下载的候选。')
    for idx, sub in enumerate(subjects, 1):
        lines.append(f"### {idx}. [{sub['collection']}] {sub['show']}  — {sub['episode_count']} 个候选")
        for item in sub['items']:
            lines.append(f"- `{item['target']}`")
            lines.append(f"  - {item['title']}")
        lines.append('')
    lines.append('确认方式：回复“确认明日下载”后，我会安排下一天 00:10 只下载这 3 部番。')
    PLAN_MD.write_text('\n'.join(lines), encoding='utf-8')
    print(PLAN_MD.read_text(encoding='utf-8'))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
