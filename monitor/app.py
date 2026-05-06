#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template_string

app = Flask(__name__)

ROOT = Path('/opt/anime-stack')
STATE_FILE = ROOT / 'state/bangumi-sync-state.json'
CATALOG_FILE = ROOT / 'state/bangumi-collections-catalog.json'
CONFIG_FILE = ROOT / 'config/bangumi-sync.json'
PLAN_FILE = ROOT / 'state/guangya-download-plan.json'
LOG_FILE = ROOT / 'state/anime-pipeline.log'

_matrix_cache = {"ts": 0.0, "data": None}


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def fmt_dt(value: str) -> str:
    if not value:
        return '-'
    s = str(value).replace('T', ' ')
    s = re.sub(r'\.\d+', '', s)
    s = s.replace('+08:00', '').replace('+00:00', ' UTC')
    return s[:19]


def parse_subgroup(title: str, fallback: str = '') -> str:
    title = title or ''
    for pat in [r'^\s*【([^】]{1,80})】', r'^\s*\[([^\]]{1,80})\]', r'^\s*([^\s\[【]{2,40}字幕组)']:
        m = re.search(pat, title)
        if m:
            raw = m.group(1).strip()
            raw = re.sub(r'\s+', ' ', raw)
            return raw[:80]
    return fallback or '-'


def episode_key(name: str) -> str:
    n = (name or '').rsplit('/', 1)[-1]
    n = re.sub(r'\.(mkv|mp4|avi|mov)$', '', n, flags=re.I)
    if not n:
        return '-'
    m = re.match(r'^(NCOP|NCED)(\d*)$', n, flags=re.I)
    if m:
        return (m.group(1).upper() + (m.group(2) or '')).upper()
    m = re.match(r'^(SP|OVA|OAD)(\d+)$', n, flags=re.I)
    if m:
        return f'{m.group(1).upper()}{int(m.group(2)):02d}'
    m = re.match(r'^(\d{1,3})(?:-|$)', n)
    if m:
        return f'{int(m.group(1)):02d}'
    return n[:20]


def target_key(row: dict) -> str:
    return f"{row.get('target_dir','')}/{row.get('target_name','')}"


def normalize_collection(label: str) -> str:
    return {'wish': '想看', 'collect': '看过', 'doing': '在看', '想看': '想看', '看过': '看过', '在看': '在看'}.get(str(label), str(label or '-'))


def build_download_matrix(refresh: bool = False) -> dict:
    if not refresh and _matrix_cache.get('data') and time.time() - float(_matrix_cache.get('ts') or 0) < 20:
        data = dict(_matrix_cache['data'])
        data['cached'] = True
        return data

    state = read_json(STATE_FILE, {})
    catalog = read_json(CATALOG_FILE, {})
    config = read_json(CONFIG_FILE, {})
    plan = read_json(PLAN_FILE, {})
    subjects = catalog.get('subjects') or state.get('tracked_subjects') or {}
    mapped = state.get('subject_to_mikan') or {}
    subgroup_lock = state.get('subject_subgroups') or {}
    tasks = state.get('cloud_tasks') or {}
    plan_submitted = plan.get('submitted') or []
    plan_unresolved = plan.get('unresolved') or []

    actual_rows = []
    actual_keys = set()
    subject_has_actual = set()
    for guid, task in tasks.items():
        if not isinstance(task, dict):
            continue
        sid = str(task.get('subject_id') or '')
        title = task.get('source_title') or task.get('title') or guid
        status = str(task.get('status') or 'submitted')
        kind = 'WAIT'
        if status == 'completed':
            kind = 'DONE'
        elif status == 'running':
            kind = 'RUN'
        elif status == 'failed' or status.startswith('status_'):
            kind = 'FAIL'
        row = {
            'kind': kind,
            'status': status,
            'subject_id': sid,
            'show': task.get('show') or (subjects.get(sid, {}) or {}).get('title') or '-',
            'collection': normalize_collection(task.get('category_dir') or task.get('collection')),
            'subgroup': parse_subgroup(title, subgroup_lock.get(sid, '')),
            'episode': episode_key(task.get('target_name') or ''),
            'target_name': task.get('target_name') or '-',
            'target_dir': task.get('target_dir') or '-',
            'source_title': title,
            'task_id': task.get('task_id') or '-',
            'submitted_at': fmt_dt(task.get('submitted_at') or ''),
            'torrent_url': task.get('torrent_url') or '',
        }
        actual_rows.append(row)
        actual_keys.add(target_key(task))
        if sid:
            subject_has_actual.add(sid)

    plan_rows = []
    subject_has_plan = set()
    for item in plan_submitted:
        if not isinstance(item, dict):
            continue
        if target_key(item) in actual_keys:
            continue
        sid = str(item.get('subject_id') or '')
        title = item.get('title') or item.get('source_title') or ''
        row = {
            'kind': 'NEXT',
            'status': 'planned',
            'subject_id': sid,
            'show': item.get('show') or (subjects.get(sid, {}) or {}).get('title') or '-',
            'collection': normalize_collection(item.get('category_dir') or item.get('collection')),
            'subgroup': parse_subgroup(title, subgroup_lock.get(sid, '')),
            'episode': episode_key(item.get('target_name') or ''),
            'target_name': item.get('target_name') or '-',
            'target_dir': item.get('target_dir') or '-',
            'source_title': title,
            'task_id': '-',
            'submitted_at': '-',
            'torrent_url': item.get('torrent_url') or '',
        }
        plan_rows.append(row)
        if sid:
            subject_has_plan.add(sid)

    backlog_rows = []
    download_types = {int(x) for x in config.get('download_collection_types', [1, 2, 3])}
    for sid, s in subjects.items():
        sid = str(sid)
        ctype = int((s or {}).get('collection_type') or 0)
        if ctype not in download_types:
            continue
        if sid in subject_has_actual or sid in subject_has_plan:
            continue
        if sid in mapped:
            backlog_rows.append({
                'kind': 'SCAN', 'status': 'mapped', 'subject_id': sid,
                'show': (s or {}).get('title') or (s or {}).get('name_cn') or (s or {}).get('name') or sid,
                'collection': normalize_collection((s or {}).get('collection_label')),
                'subgroup': subgroup_lock.get(sid, '-'), 'episode': 'SCAN', 'target_name': '-', 'target_dir': '-',
                'source_title': f"Mikan #{mapped.get(sid)}", 'task_id': '-', 'submitted_at': '-', 'torrent_url': '',
            })

    unresolved_rows = []
    unresolved_ids = {str(x.get('subject_id')) for x in plan_unresolved if isinstance(x, dict)}
    for sid in unresolved_ids:
        s = subjects.get(sid, {}) or {}
        unresolved_rows.append({
            'kind': 'MISS', 'status': 'unresolved', 'subject_id': sid,
            'show': s.get('title') or s.get('name_cn') or s.get('name') or sid,
            'collection': normalize_collection(s.get('collection_label')),
            'subgroup': '-', 'episode': '-', 'target_name': '-', 'target_dir': '-',
            'source_title': '-', 'task_id': '-', 'submitted_at': '-', 'torrent_url': '',
        })

    rows = actual_rows + plan_rows + backlog_rows + unresolved_rows
    priority = {'RUN': 0, 'WAIT': 1, 'FAIL': 2, 'NEXT': 3, 'SCAN': 4, 'MISS': 5, 'DONE': 6}
    rows.sort(key=lambda r: (priority.get(r['kind'], 9), r['collection'], r['show'], r['episode']))

    by_show = {}
    for r in rows:
        sid = r['subject_id'] or r['show']
        g = by_show.setdefault(sid, {
            'subject_id': sid, 'show': r['show'], 'collection': r['collection'], 'subgroups': Counter(),
            'done': 0, 'run': 0, 'wait': 0, 'fail': 0, 'next': 0, 'scan': 0, 'miss': 0, 'episodes': [],
        })
        g['subgroups'][r['subgroup']] += 1
        k = r['kind'].lower()
        if k in g:
            g[k] += 1
        g['episodes'].append(r['episode'])
    shows = []
    for g in by_show.values():
        total = g['done'] + g['run'] + g['wait'] + g['fail'] + g['next']
        g['total'] = total
        g['percent'] = round((g['done'] / total) * 100, 1) if total else 0
        g['subgroup'] = g['subgroups'].most_common(1)[0][0] if g['subgroups'] else '-'
        del g['subgroups']
        g['episodes'] = sorted(set(g['episodes']))[:80]
        shows.append(g)
    shows.sort(key=lambda x: (-x['run'], -x['wait'], -x['next'], x['collection'], x['show']))

    counts = Counter(r['kind'] for r in rows)
    subgroup_counts = Counter(r['subgroup'] for r in rows if r['subgroup'] and r['subgroup'] != '-')
    collection_counts = Counter(r['collection'] for r in rows)
    daily_limit = int(config.get('max_new_cloud_tasks_per_run') or 0)
    today = datetime.now().strftime('%Y-%m-%d')
    today_count = sum(1 for r in actual_rows if str(r.get('submitted_at','')).startswith(today))
    plan_mtime = fmt_dt(datetime.fromtimestamp(PLAN_FILE.stat().st_mtime).isoformat()) if PLAN_FILE.exists() else '-'

    data = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'last_run': fmt_dt(state.get('last_run') or ''),
        'plan_mtime': plan_mtime,
        'tracked': len(subjects),
        'mapped': len(mapped),
        'download_types': sorted(download_types),
        'daily_limit': daily_limit,
        'today': today_count,
        'quota_percent': round(today_count / daily_limit * 100, 1) if daily_limit else 0,
        'counts': dict(counts),
        'submitted_count': len(actual_rows),
        'planned_count': len(plan_rows),
        'backlog_count': len(backlog_rows),
        'unresolved_count': len(unresolved_rows),
        'subgroups': [{'name': k, 'count': v} for k, v in subgroup_counts.most_common(20)],
        'collections': dict(collection_counts),
        'rows': rows[:3000],
        'shows': shows[:1000],
        'cached': False,
    }
    _matrix_cache.update({'ts': time.time(), 'data': data})
    return data


def get_live_status() -> dict:
    procs = []
    try:
        out = subprocess.run(['bash','-lc',"ps -eo pid=,args= | grep -E 'bangumi_cloud_download.py|run_anime_pipeline' | grep -v grep"], capture_output=True, text=True, timeout=5)
        for line in out.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2:
                procs.append({'pid': parts[0], 'cmd': parts[1][:160]})
    except Exception:
        pass
    tail = []
    if LOG_FILE.exists():
        try:
            tail = LOG_FILE.read_text(encoding='utf-8', errors='ignore').splitlines()[-30:]
        except Exception:
            tail = []
    return {'running': bool(procs), 'processes': procs, 'tail': tail, 'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}


HTML = r'''
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>GuangYa Anime</title>
<style>
:root{--bg:#090b0f;--panel:#11151c;--line:#222936;--text:#e8edf5;--muted:#8a94a6;--blue:#4aa3ff;--green:#48d17c;--yellow:#ffbd45;--red:#ff5c68;--purple:#b28cff;--cyan:#45d4ff}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}.wrap{max-width:1680px;margin:0 auto;padding:20px}header{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--line);padding-bottom:14px;margin-bottom:16px}h1{font-size:20px;margin:0;color:var(--blue);letter-spacing:.12em}.time{color:var(--muted);font-size:12px}.kpis{display:grid;grid-template-columns:repeat(8,minmax(0,1fr));gap:10px;margin-bottom:14px}.kpi{border:1px solid var(--line);background:linear-gradient(180deg,#121821,#0d1118);border-radius:12px;padding:12px}.kpi b{display:block;font-size:24px;line-height:1}.kpi span{display:block;color:var(--muted);font-size:11px;margin-top:6px;letter-spacing:.1em}.main{display:grid;grid-template-columns:1.1fr .9fr;gap:14px;margin-bottom:14px}.panel{border:1px solid var(--line);background:var(--panel);border-radius:14px;padding:14px;min-width:0}.title{font-size:12px;color:var(--blue);letter-spacing:.14em;font-weight:800;margin-bottom:10px}.bar{height:10px;background:#1a202b;border-radius:99px;overflow:hidden}.fill{height:100%;background:linear-gradient(90deg,var(--blue),var(--green));width:0}.stack{display:flex;height:20px;border-radius:99px;overflow:hidden;background:#1a202b}.seg{height:100%}.seg.done{background:var(--green)}.seg.run{background:var(--cyan)}.seg.wait{background:var(--yellow)}.seg.next{background:var(--blue)}.seg.fail{background:var(--red)}.seg.scan{background:var(--purple)}.legend{display:flex;gap:10px;flex-wrap:wrap;margin-top:10px;color:var(--muted);font-size:12px}.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:5px}.toolbar{display:grid;grid-template-columns:2fr repeat(4,1fr);gap:8px;margin-bottom:10px}input,select{background:#0b0f15;border:1px solid var(--line);color:var(--text);border-radius:9px;padding:9px;font-size:13px}.tablebox{border:1px solid var(--line);border-radius:12px;overflow:auto;max-height:680px}table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid var(--line);padding:9px 8px;text-align:left;white-space:nowrap}th{position:sticky;top:0;background:#101721;color:var(--muted);z-index:2;font-size:11px;letter-spacing:.08em}td.titlecell{max-width:520px;overflow:hidden;text-overflow:ellipsis}td.showcell{max-width:260px;overflow:hidden;text-overflow:ellipsis}.tag{display:inline-flex;align-items:center;border-radius:999px;padding:3px 8px;font-size:11px;font-weight:800}.DONE{background:rgba(72,209,124,.14);color:var(--green)}.RUN{background:rgba(69,212,255,.14);color:var(--cyan)}.WAIT{background:rgba(255,189,69,.14);color:var(--yellow)}.NEXT{background:rgba(74,163,255,.14);color:var(--blue)}.FAIL{background:rgba(255,92,104,.14);color:var(--red)}.SCAN{background:rgba(178,140,255,.14);color:var(--purple)}.MISS{background:rgba(255,92,104,.14);color:var(--red)}.subgrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.subitem{display:flex;align-items:center;gap:8px;margin:8px 0}.subbar{flex:1;height:8px;background:#1a202b;border-radius:99px;overflow:hidden}.subfill{height:100%;background:var(--blue)}.shows{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px;max-height:360px;overflow:auto}.show{border:1px solid var(--line);border-radius:10px;padding:10px;background:#0d1219}.showtop{display:flex;justify-content:space-between;gap:8px;font-size:13px}.showname{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.eps{color:var(--muted);font-size:11px;margin-top:6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}@media(max-width:1000px){.kpis{grid-template-columns:repeat(4,1fr)}.main{grid-template-columns:1fr}.toolbar{grid-template-columns:1fr 1fr}.subgrid{grid-template-columns:1fr}}@media(max-width:640px){.wrap{padding:12px}.kpis{grid-template-columns:repeat(2,1fr)}.toolbar{grid-template-columns:1fr}}
</style></head><body><div class="wrap"><header><h1>GUANGYA ANIME</h1><div id="clock" class="time">-</div></header>
<div class="kpis"><div class="kpi"><b id="k-tracked">0</b><span>BGM</span></div><div class="kpi"><b id="k-mapped">0</b><span>MIKAN</span></div><div class="kpi"><b id="k-wait">0</b><span>WAIT</span></div><div class="kpi"><b id="k-run">0</b><span>RUN</span></div><div class="kpi"><b id="k-done">0</b><span>DONE</span></div><div class="kpi"><b id="k-next">0</b><span>NEXT</span></div><div class="kpi"><b id="k-scan">0</b><span>SCAN</span></div><div class="kpi"><b id="k-miss">0</b><span>MISS</span></div></div>
<div class="main"><section class="panel"><div class="title">STATUS</div><div class="stack" id="stack"></div><div class="legend" id="legend"></div><div style="margin-top:14px"><div class="time">TODAY <span id="quota-txt">0/0</span></div><div class="bar"><div id="quota-fill" class="fill"></div></div></div></section><section class="panel"><div class="title">SUBGROUP</div><div id="subgroups" class="subgrid"></div></section></div>
<section class="panel" style="margin-bottom:14px"><div class="title">SHOW</div><div id="shows" class="shows"></div></section>
<section class="panel"><div class="title">EPISODE</div><div class="toolbar"><input id="q" placeholder="FILTER"><select id="kind"><option value="">ALL</option><option>WAIT</option><option>RUN</option><option>DONE</option><option>NEXT</option><option>SCAN</option><option>MISS</option><option>FAIL</option></select><select id="collection"><option value="">COLL</option></select><select id="subgroup"><option value="">GROUP</option></select><select id="limit"><option>300</option><option>800</option><option>1500</option><option>3000</option></select></div><div class="tablebox"><table><thead><tr><th>STAT</th><th>COLL</th><th>SHOW</th><th>EP</th><th>GROUP</th><th>FILE</th><th>TITLE</th><th>TIME</th></tr></thead><tbody id="rows"></tbody></table></div></section>
</div><script>
let DATA={rows:[],shows:[],subgroups:[],counts:{},collections:{}};
const colors={DONE:'var(--green)',RUN:'var(--cyan)',WAIT:'var(--yellow)',NEXT:'var(--blue)',FAIL:'var(--red)',SCAN:'var(--purple)',MISS:'var(--red)'};
function set(id,v){const e=document.getElementById(id); if(e)e.textContent=v}
function esc(s){return String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
async function load(){const r=await fetch('/api/download_matrix'); DATA=await r.json(); render();}
function render(){const c=DATA.counts||{}; set('clock',`${DATA.generated_at||'-'} | PLAN ${DATA.plan_mtime||'-'} | RUN ${DATA.last_run||'-'}`); set('k-tracked',DATA.tracked||0); set('k-mapped',DATA.mapped||0); set('k-wait',c.WAIT||0); set('k-run',c.RUN||0); set('k-done',c.DONE||0); set('k-next',c.NEXT||0); set('k-scan',c.SCAN||0); set('k-miss',c.MISS||0); set('quota-txt',`${DATA.today||0}/${DATA.daily_limit||0}`); document.getElementById('quota-fill').style.width=Math.min(100,DATA.quota_percent||0)+'%'; renderStack(); renderFilters(); renderSubgroups(); renderShows(); renderRows();}
function renderStack(){const total=Object.values(DATA.counts||{}).reduce((a,b)=>a+b,0)||1; document.getElementById('stack').innerHTML=['DONE','RUN','WAIT','NEXT','FAIL','SCAN','MISS'].map(k=>`<div class="seg ${k.toLowerCase()}" style="width:${((DATA.counts[k]||0)/total*100).toFixed(2)}%"></div>`).join(''); document.getElementById('legend').innerHTML=['DONE','RUN','WAIT','NEXT','FAIL','SCAN','MISS'].map(k=>`<span><i class="dot" style="background:${colors[k]}"></i>${k} ${DATA.counts[k]||0}</span>`).join('')}
function renderFilters(){const coll=document.getElementById('collection'), sg=document.getElementById('subgroup'); const cv=coll.value, sv=sg.value; coll.innerHTML='<option value="">COLL</option>'+Object.keys(DATA.collections||{}).sort().map(x=>`<option>${esc(x)}</option>`).join(''); sg.innerHTML='<option value="">GROUP</option>'+(DATA.subgroups||[]).map(x=>`<option>${esc(x.name)}</option>`).join(''); coll.value=cv; sg.value=sv;}
function renderSubgroups(){const max=Math.max(1,...(DATA.subgroups||[]).map(x=>x.count)); document.getElementById('subgroups').innerHTML=(DATA.subgroups||[]).slice(0,16).map(x=>`<div class="subitem"><span style="width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(x.name)}">${esc(x.name)}</span><div class="subbar"><div class="subfill" style="width:${x.count/max*100}%"></div></div><b>${x.count}</b></div>`).join('')||'<span class="time">-</span>'}
function renderShows(){document.getElementById('shows').innerHTML=(DATA.shows||[]).slice(0,120).map(s=>`<div class="show"><div class="showtop"><span class="showname" title="${esc(s.show)}">${esc(s.show)}</span><span>${s.done||0}/${s.total||0}</span></div><div class="bar" style="margin-top:8px"><div class="fill" style="width:${Math.min(100,s.percent||0)}%"></div></div><div class="eps">${esc(s.collection)} · ${esc(s.subgroup)} · ${(s.episodes||[]).join(' ')}</div></div>`).join('')}
function renderRows(){const q=document.getElementById('q').value.toLowerCase(), kind=document.getElementById('kind').value, coll=document.getElementById('collection').value, sg=document.getElementById('subgroup').value, lim=parseInt(document.getElementById('limit').value||300); let rows=(DATA.rows||[]).filter(r=>(!kind||r.kind===kind)&&(!coll||r.collection===coll)&&(!sg||r.subgroup===sg)&&(!q||(`${r.show} ${r.episode} ${r.subgroup} ${r.target_name} ${r.source_title}`).toLowerCase().includes(q))).slice(0,lim); document.getElementById('rows').innerHTML=rows.map(r=>`<tr><td><span class="tag ${r.kind}">${r.kind}</span></td><td>${esc(r.collection)}</td><td class="showcell" title="${esc(r.show)}">${esc(r.show)}</td><td><b>${esc(r.episode)}</b></td><td>${esc(r.subgroup)}</td><td>${esc(r.target_name)}</td><td class="titlecell" title="${esc(r.source_title)}">${esc(r.source_title)}</td><td>${esc(r.submitted_at)}</td></tr>`).join('')||'<tr><td colspan="8" class="time">-</td></tr>'}
['q','kind','collection','subgroup','limit'].forEach(id=>document.addEventListener('input',e=>{if(e.target&&e.target.id===id)renderRows()}));
load(); setInterval(load,20000);
</script></body></html>
'''


@app.route('/')
def index():
    return render_template_string(HTML)


@app.route('/api/download_matrix')
def api_download_matrix():
    return jsonify(build_download_matrix())


@app.route('/api/guangya_progress')
def api_guangya_progress():
    d = build_download_matrix()
    c = d.get('counts', {})
    return jsonify({
        'total': d.get('submitted_count', 0),
        'completed': c.get('DONE', 0),
        'running': c.get('RUN', 0),
        'submitted': c.get('WAIT', 0),
        'failed': c.get('FAIL', 0),
        'planned': c.get('NEXT', 0),
        'today': d.get('today', 0),
        'daily_limit': d.get('daily_limit', 0),
        'quota_percent': d.get('quota_percent', 0),
        'rows': d.get('shows', [])[:80],
        'updated_at': d.get('generated_at'),
    })


@app.route('/api/live_status')
def api_live_status():
    return jsonify(get_live_status())


@app.route('/api/full_status')
def api_full_status():
    return jsonify({'matrix': build_download_matrix(), 'live': get_live_status()})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
