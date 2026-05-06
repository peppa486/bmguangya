#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path('/opt/anime-stack')
OUT = ROOT / 'state/guangya-download-plan.json'
RAW = ROOT / 'state/guangya-download-plan.json.raw'
CMD = [
    '/opt/guangya-webdav/venv/bin/python',
    str(ROOT / 'scripts/bangumi_cloud_download.py'),
    '--config', str(ROOT / 'config/bangumi-sync.json'),
    '--dry-run',
]


def extract_json(text: str) -> dict:
    for idx, ch in enumerate(text):
        if ch != '{':
            continue
        try:
            return json.loads(text[idx:])
        except json.JSONDecodeError:
            continue
    raise ValueError('no JSON summary found in dry-run output')


def main() -> int:
    proc = subprocess.run(CMD, cwd=str(ROOT), capture_output=True, text=True, timeout=900)
    combined = (proc.stdout or '') + ('\n' + proc.stderr if proc.stderr else '')
    RAW.write_text(combined, encoding='utf-8')
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)
    data = extract_json(combined)
    tmp = OUT.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(OUT)
    RAW.unlink(missing_ok=True)
    # This script runs bangumi_cloud_download.py in --dry-run mode to build a
    # dashboard/cache plan. Do not call these real submissions; that previously
    # made logs look like 200 tasks had been submitted even when GuangYa quota was
    # exhausted before the real run created anything.
    print(json.dumps({
        'planned_count': data.get('submitted_count', 0),
        'unresolved_count': data.get('unresolved_count', 0),
        'tracked_count': data.get('tracked_count', 0),
        'dry_run': True,
    }, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
