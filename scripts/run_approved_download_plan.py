#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path('/opt/anime-stack')
CONFIG = ROOT / 'config/bangumi-sync.json'
PYTHON = '/opt/guangya-webdav/venv/bin/python'
SYNC = ROOT / 'scripts/bangumi_cloud_download.py'
PLAN_JSON = ROOT / 'state/pending-download-plan.json'


def main() -> int:
    if not PLAN_JSON.exists():
        raise SystemExit('no pending plan file')
    plan = json.loads(PLAN_JSON.read_text(encoding='utf-8'))
    if plan.get('status') not in {'approved', 'scheduled'}:
        raise SystemExit(f"plan is not approved: {plan.get('status')}")
    subject_ids = [str(x) for x in plan.get('subject_ids') or []]
    if not subject_ids:
        raise SystemExit('approved plan has no subject_ids')

    cfg = json.loads(CONFIG.read_text(encoding='utf-8'))
    cfg['dry_run'] = False
    cfg['download_subject_ids'] = subject_ids
    cfg['max_download_subjects_per_run'] = min(int(cfg.get('daily_plan_subject_limit') or 3), len(subject_ids))
    cfg['max_new_cloud_tasks_per_run'] = int(cfg.get('approved_plan_task_limit') or 200)
    cfg['max_episodes_per_subject_per_run'] = int(cfg.get('daily_plan_episodes_per_subject') or 1)
    cfg['episode_selection_mode'] = cfg.get('episode_selection_mode') or 'latest'

    with tempfile.NamedTemporaryFile('w', encoding='utf-8', suffix='.json', delete=False) as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        tmp_config = f.name

    plan['status'] = 'running'
    plan['started_at'] = datetime.now().astimezone().isoformat()
    PLAN_JSON.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')

    proc = subprocess.run([PYTHON, str(SYNC), '--config', tmp_config], cwd=str(ROOT))
    plan['finished_at'] = datetime.now().astimezone().isoformat()
    plan['status'] = 'executed' if proc.returncode == 0 else 'failed'
    plan['returncode'] = proc.returncode
    PLAN_JSON.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
    return proc.returncode

if __name__ == '__main__':
    raise SystemExit(main())
