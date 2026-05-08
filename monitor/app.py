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

from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

ROOT = Path('/opt/anime-stack')
STATE_FILE = ROOT / 'state/bangumi-sync-state.json'
CATALOG_FILE = ROOT / 'state/bangumi-collections-catalog.json'
CONFIG_FILE = ROOT / 'config/bangumi-sync.json'
PLAN_FILE = ROOT / 'state/guangya-download-plan.json'
PENDING_PLAN_FILE = ROOT / 'state/pending-download-plan.json'
CONTROL_CANDIDATES_FILE = ROOT / 'state/download-control-candidates.json'
LOG_FILE = ROOT / 'state/anime-pipeline.log'
PROGRESS_FILE = ROOT / 'state/sync-progress.json'

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
</style></head><body><div class="wrap"><header><h1>GUANGYA ANIME</h1><div style="display:flex;gap:14px;align-items:center"><a href="/download-control" style="color:var(--blue);text-decoration:none;font-size:12px;letter-spacing:.12em;font-weight:800">下载控制台</a><div id="clock" class="time">-</div></div></header>
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



@app.route('/api/sync_progress')
def api_sync_progress():
    data = read_json(PROGRESS_FILE, {})
    if not data:
        data = {'status': 'idle', 'current_subject': 0, 'total_subjects': 0, 'submitted_count': 0, 'max_tasks': 0, 'current_show': '', 'updated_at': ''}
    return jsonify(data)

@app.route('/api/full_status')
def api_full_status():
    return jsonify({'matrix': build_download_matrix(), 'live': get_live_status()})



CONTROL_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>下载控制台</title>
<style>
:root{
  --bg:#0f1115;--panel:#151820;--panel2:#101318;--line:#2a2f3a;--line2:#20252e;
  --text:#e7e9ee;--muted:#9aa1ad;--weak:#68707d;--focus:#7aa2ff;
  --ok:#8ccf9b;--warn:#d8b76c;--bad:#e17b85;--row:#171b23;--row2:#131720;
  --radius:8px;--font:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei","Segoe UI",sans-serif;
  --mono:"SFMono-Regular","Cascadia Code","Menlo",monospace;
}
*{box-sizing:border-box}html,body{height:100%}body{margin:0;background:var(--bg);color:var(--text);font-family:var(--font);font-size:14px}a{color:inherit}.app{height:100vh;display:grid;grid-template-rows:52px 1fr}.top{border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 14px;background:#12151b}.brand{display:flex;align-items:center;gap:14px;min-width:0}.brand h1{font-size:15px;margin:0;font-weight:650;letter-spacing:.02em}.counts{display:flex;gap:14px;color:var(--muted);font-size:12px}.counts b{color:var(--text);font-weight:650}.actions{display:flex;align-items:center;gap:8px}.btn{height:30px;border:1px solid var(--line);background:#171b23;color:var(--text);border-radius:6px;padding:0 10px;font-size:12px}.btn:hover{background:#1c222c}.btn.primary{background:#e7e9ee;color:#111318;border-color:#e7e9ee}.btn.danger{color:var(--bad)}.main{min-height:0;display:grid;grid-template-columns:240px minmax(0,1fr) 340px}.side{border-right:1px solid var(--line);background:#11141a;padding:12px;overflow:auto}.right{border-left:1px solid var(--line);background:#11141a;display:grid;grid-template-rows:auto 1fr auto;min-width:0}.section{margin-bottom:16px}.label{font-size:11px;letter-spacing:.08em;color:var(--weak);margin:0 0 8px}.field{width:100%;height:32px;background:#0d1015;border:1px solid var(--line);color:var(--text);border-radius:6px;padding:0 9px;outline:none}.field:focus{border-color:var(--focus)}.stack{display:grid;gap:8px}.line{display:flex;align-items:center;justify-content:space-between;color:var(--muted);font-size:12px}.line strong{color:var(--text);font-weight:600}.check{display:flex;gap:8px;align-items:center;color:var(--muted);font-size:12px}.content{min-width:0;min-height:0;display:grid;grid-template-rows:42px 1fr}.bar{border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 12px;background:#12151b}.status{color:var(--muted);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.tableWrap{min-height:0;overflow:auto}.group{border-bottom:1px solid var(--line);background:#141820}.groupHead{height:38px;display:grid;grid-template-columns:34px 64px minmax(220px,1fr) 92px 86px 120px;gap:8px;align-items:center;padding:0 12px;color:var(--muted);position:sticky;top:0;background:#141820;z-index:1}.groupTitle{color:var(--text);font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.tag{font-size:12px;color:var(--muted)}.tag.doing{color:var(--ok)}.tag.wish{color:#b8a6df}.tag.done{color:var(--muted)}.row{min-height:36px;display:grid;grid-template-columns:34px 64px minmax(260px,1fr) minmax(300px,1.2fr) 96px 100px;gap:8px;align-items:center;padding:0 12px;border-top:1px solid var(--line2);background:var(--row2)}.row:nth-child(odd){background:var(--row)}.row:hover{background:#1a2029}.row.disabled{opacity:.45}.cb{width:16px;height:16px;accent-color:#dfe3ea}.ep{font-family:var(--mono);font-size:13px;color:var(--text)}.target{font-family:var(--mono);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.title{color:var(--muted);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.subgroup,.state{font-size:12px;color:var(--weak);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.state.ok{color:var(--ok)}.state.warn{color:var(--warn)}.state.bad{color:var(--bad)}.empty{padding:24px;color:var(--weak);text-align:center}.rightHead{padding:12px;border-bottom:1px solid var(--line)}.selectedList{overflow:auto;padding:8px}.selectedItem{border-bottom:1px solid var(--line2);padding:8px 4px}.selectedItem code{display:block;font-family:var(--mono);font-size:12px;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.selectedItem span{display:block;margin-top:3px;color:var(--weak);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.rightFoot{border-top:1px solid var(--line);padding:10px 12px;color:var(--weak);font-size:12px;line-height:1.5}.toast{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);background:#e7e9ee;color:#111318;border-radius:6px;padding:8px 12px;font-size:12px;opacity:0;pointer-events:none}.toast.show{opacity:1}@media(max-width:1100px){.main{grid-template-columns:1fr}.side,.right{display:none}.row{grid-template-columns:30px 52px minmax(160px,1fr)}.title,.subgroup,.state{display:none}.groupHead{grid-template-columns:30px 52px 1fr 80px}.groupHead .hideSmall{display:none}}
</style>
</head>
<body>
<div class="app">
<header class="top"><div class="brand"><h1>下载控制台</h1><div class="counts"><span>番剧 <b id="mShows">0</b></span><span>候选 <b id="mFiles">0</b></span><span>已选 <b id="mSelected">0</b></span><span id="planState">-</span></div></div><div class="actions"><a class="btn" href="/">监控</a><button class="btn" id="refreshBtn">重新扫描</button><button class="btn" id="saveBtn">保存</button><button class="btn primary" id="approveBtn">批准执行</button></div></header>
<main class="main">
<aside class="side"><div class="section"><p class="label">筛选</p><div class="stack"><input id="q" class="field" placeholder="搜索番剧 / 文件 / 字幕组"><select id="collection" class="field"><option value="">全部收藏</option></select><select id="view" class="field"><option value="all">全部候选</option><option value="selected">只看已选</option><option value="safe">只看可选</option></select><label class="check"><input type="checkbox" id="hideExisting" checked>隐藏已存在/跳过</label></div></div><div class="section"><p class="label">操作</p><div class="stack"><button class="btn" id="selectVisible">选择当前显示</button><button class="btn danger" id="clearSelected">清空选择</button></div></div><div class="section"><p class="label">规则</p><div class="stack"><div class="line"><span>执行粒度</span><strong>具体文件</strong></div><div class="line"><span>多文件种子</span><strong>fileIndexes</strong></div><div class="line"><span>未批准计划</span><strong>不执行</strong></div></div></div></aside>
<section class="content"><div class="bar"><div id="status" class="status">加载中</div><div class="actions"><button class="btn" id="collapseAll">折叠全部</button><button class="btn" id="expandAll">展开全部</button></div></div><div id="table" class="tableWrap"></div></section>
<aside class="right"><div class="rightHead"><p class="label">已选文件</p><div class="line"><span>将提交</span><strong id="basketCount">0</strong></div></div><div id="basket" class="selectedList"><div class="empty">未选择</div></div><div class="rightFoot">保存会写入待确认计划；批准后才允许定时执行。执行脚本只读取已批准的具体文件白名单。</div></aside>
</main></div><div id="toast" class="toast"></div>
<script>
let DATA={subjects:[],plan:{}};let selected=new Set();let collapsed=new Set();
const $=id=>document.getElementById(id);const esc=s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
function toast(s){const t=$('toast');t.textContent=s;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),1800)}
function subgroup(t){let m=String(t||'').match(/^\s*[【\[]([^】\]]{1,50})/);return m?m[1]:'-'}
function epOf(n){n=String(n||'').replace(/\.[^.]+$/,'');let m=n.match(/(\d{1,3})/);return m?String(parseInt(m[1],10)).padStart(2,'0'):n.slice(0,8)}
function safe(it){return it.selectable!==false&&!it.exists&&!it.skipped}
function flat(){return(DATA.subjects||[]).flatMap(s=>(s.items||[]).map(it=>({...it,subject:s})))}
async function load(){let r=await fetch('/api/download_control');DATA=await r.json();selected=new Set(DATA.selected_targets||[]);render()}
function renderFilters(){let cur=$('collection').value;let xs=[...new Set((DATA.subjects||[]).map(s=>s.collection).filter(Boolean))];$('collection').innerHTML='<option value="">全部收藏</option>'+xs.map(x=>`<option>${esc(x)}</option>`).join('');$('collection').value=cur}
function filtered(){const q=$('q').value.toLowerCase(),coll=$('collection').value,view=$('view').value,hide=$('hideExisting').checked;return(DATA.subjects||[]).map(s=>({...s,items:(s.items||[]).filter(it=>{let hay=`${s.show} ${it.target} ${it.title} ${subgroup(it.title)}`.toLowerCase();if(q&&!hay.includes(q))return false;if(coll&&s.collection!==coll)return false;if(hide&&(it.exists||it.skipped))return false;if(view==='selected'&&!selected.has(it.target))return false;if(view==='safe'&&!safe(it))return false;return true})})).filter(s=>s.items.length)}
function render(){renderFilters();let all=flat();$('mShows').textContent=(DATA.subjects||[]).length;$('mFiles').textContent=all.length;$('mSelected').textContent=selected.size;$('basketCount').textContent=selected.size;$('planState').textContent=DATA.plan?.status||'-';let shown=filtered();$('status').textContent=`显示 ${shown.reduce((n,s)=>n+s.items.length,0)} / ${all.length} 个文件`;renderTable(shown);renderBasket()}
function cls(c){return c==='在看'?'doing':c==='想看'?'wish':'done'}
function renderTable(subjects){$('table').innerHTML=subjects.map(s=>{let open=!collapsed.has(String(s.subject_id));return `<div class="group"><div class="groupHead"><button class="btn" onclick="toggleGroup('${esc(s.subject_id)}')">${open?'−':'+'}</button><span class="tag ${cls(s.collection)}">${esc(s.collection||'-')}</span><span class="groupTitle" title="${esc(s.show)}">${esc(s.show)}</span><span class="hideSmall">${s.items.length} 个</span><button class="btn hideSmall" onclick="selectShow('${esc(s.subject_id)}')">选择</button><button class="btn hideSmall" onclick="clearShow('${esc(s.subject_id)}')">取消</button></div>${open?s.items.map(it=>row(it)).join(''):''}</div>`}).join('')||'<div class="empty">无匹配文件</div>'}
function row(it){let ok=safe(it),checked=selected.has(it.target),reason=it.exists?'已存在':(it.skipped_reason||it.reason||(ok?'可选':'跳过'));return `<label class="row ${ok?'':'disabled'}"><input class="cb" type="checkbox" ${checked?'checked':''} ${ok?'':'disabled'} onchange="toggleTarget('${encodeURIComponent(it.target)}',this.checked)"><span class="ep">${esc(epOf(it.target_name||it.target))}</span><span class="target" title="${esc(it.target)}">${esc(it.target)}</span><span class="title" title="${esc(it.title)}">${esc(it.title)}</span><span class="subgroup">${esc(subgroup(it.title))}</span><span class="state ${ok?'ok':it.exists?'warn':'bad'}">${esc(reason)}</span></label>`}
function renderBasket(){let xs=flat().filter(it=>selected.has(it.target));$('basket').innerHTML=xs.length?xs.map(it=>`<div class="selectedItem"><code>${esc(it.target)}</code><span>${esc(it.title)}</span></div>`).join(''):'<div class="empty">未选择</div>'}
function toggleGroup(sid){sid=String(sid);collapsed.has(sid)?collapsed.delete(sid):collapsed.add(sid);render()}function toggleTarget(t,on){t=decodeURIComponent(t);on?selected.add(t):selected.delete(t);render()}function selectShow(sid){flat().filter(x=>String(x.subject.subject_id)===String(sid)&&safe(x)).forEach(x=>selected.add(x.target));render()}function clearShow(sid){flat().filter(x=>String(x.subject.subject_id)===String(sid)).forEach(x=>selected.delete(x.target));render()}
async function save(status){let targets=[...selected];if(!targets.length){toast('未选择文件');return}let r=await fetch('/api/download_control/plan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({targets,status})});let d=await r.json();if(!d.ok){toast(d.error||'保存失败');return}DATA=d.data;selected=new Set(DATA.selected_targets||targets);render();toast(status==='approved'?'已批准':'已保存')}
$('saveBtn').onclick=()=>save('pending_confirmation');$('approveBtn').onclick=()=>save('approved');$('clearSelected').onclick=()=>{selected.clear();render()};$('selectVisible').onclick=()=>{filtered().flatMap(s=>s.items).filter(safe).forEach(it=>selected.add(it.target));render()};$('collapseAll').onclick=()=>{filtered().forEach(s=>collapsed.add(String(s.subject_id)));render()};$('expandAll').onclick=()=>{collapsed.clear();render()};$('refreshBtn').onclick=async()=>{toast('扫描中');let r=await fetch('/api/download_control/regenerate',{method:'POST'});let d=await r.json();if(!d.ok){toast(d.error||'扫描失败');return}DATA=d.data;selected=new Set(DATA.selected_targets||[]);render();toast('已刷新')};['q','collection','view','hideExisting'].forEach(id=>$(id).addEventListener('input',render));load();
</script>
</body>
</html>
"""


def _subject_order_key(s: dict):
    order = {'在看': 0, '想看': 1, '看过': 2}
    return (order.get(str(s.get('collection')), 9), str(s.get('show') or ''))


def _load_control_source() -> dict:
    data = read_json(CONTROL_CANDIDATES_FILE, {})
    if data.get('subjects'):
        return data
    return read_json(PENDING_PLAN_FILE, {})


def _normalize_plan_subjects(plan: dict) -> list[dict]:
    subjects = []
    for sub in plan.get('subjects') or []:
        if not isinstance(sub, dict):
            continue
        items = []
        for it in sub.get('items') or []:
            if not isinstance(it, dict):
                continue
            target = str(it.get('target') or '').strip('/')
            if not target:
                target_dir = str(it.get('target_dir') or '').strip('/')
                target_name = str(it.get('target_name') or '').strip('/')
                target = f'{target_dir}/{target_name}'.strip('/')
            if not target:
                continue
            target_name = str(it.get('target_name') or target.rsplit('/', 1)[-1])
            items.append({
                'title': it.get('title') or it.get('source_title') or '',
                'target': target,
                'target_name': target_name,
                'torrent_url': it.get('torrent_url') or '',
                'exists': bool(it.get('exists')),
                'skipped': bool(it.get('skipped')),
                'skipped_reason': it.get('skipped_reason') or it.get('reason') or '',
                'selectable': False if it.get('selectable') is False else True,
            })
        subjects.append({
            'subject_id': str(sub.get('subject_id') or ''),
            'show': sub.get('show') or sub.get('title') or '-',
            'collection': normalize_collection(sub.get('collection') or sub.get('category_dir') or '-'),
            'category_dir': sub.get('category_dir') or normalize_collection(sub.get('collection') or ''),
            'show_dir': sub.get('show_dir') or sub.get('show') or '-',
            'episode_count': len(items),
            'items': items,
        })
    subjects.sort(key=_subject_order_key)
    return subjects


def build_download_control_payload() -> dict:
    plan = _load_control_source()
    pending = read_json(PENDING_PLAN_FILE, {})
    selected = []
    for sub in pending.get('subjects') or []:
        for it in sub.get('items') or []:
            target = str(it.get('target') or '').strip('/')
            if target:
                selected.append(target)
    subjects = _normalize_plan_subjects(plan)
    return {
        'generated_at': fmt_dt(plan.get('created_at') or datetime.now().astimezone().isoformat()),
        'plan': {'status': pending.get('status') or plan.get('status') or 'none', 'created_at': pending.get('created_at') or plan.get('created_at')},
        'subjects': subjects,
        'selected_targets': selected if selected else [it['target'] for s in subjects for it in s.get('items', []) if it.get('preselected')],
    }


def _items_by_target(subjects: list[dict]) -> dict:
    out = {}
    for s in subjects:
        for it in s.get('items') or []:
            out[it.get('target')] = (s, it)
    return out


def save_download_control_plan(targets: list[str], status: str) -> dict:
    payload = build_download_control_payload()
    subjects = payload.get('subjects') or []
    index = _items_by_target(subjects)
    clean_targets = []
    for t in targets:
        t = str(t or '').strip('/')
        if t and t in index and t not in clean_targets:
            clean_targets.append(t)
    if not clean_targets:
        raise ValueError('没有可保存的文件目标')
    grouped = {}
    for target in clean_targets:
        s, it = index[target]
        sid = str(s.get('subject_id') or '')
        grouped.setdefault(sid, {'subject_id': sid, 'show': s.get('show'), 'collection': s.get('collection'), 'category_dir': s.get('category_dir'), 'show_dir': s.get('show_dir'), 'items': []})
        grouped[sid]['items'].append({k: it.get(k) for k in ['title', 'target', 'target_name', 'torrent_url']})
    plan = {
        'created_at': datetime.now().astimezone().isoformat(),
        'status': status if status in {'pending_confirmation', 'approved', 'scheduled'} else 'pending_confirmation',
        'policy': {
            'mode': 'exact_file_whitelist',
            'source': 'monitor.ecust.cc/download-control',
            'safety': ['reject_batch_torrents', 'remote_duplicate_check', 'single_fileIndexes_for_multifile'],
        },
        'subject_ids': list(grouped.keys()),
        'subjects': list(grouped.values()),
        'summary': {'subject_count': len(grouped), 'file_count': len(clean_targets)},
    }
    PENDING_PLAN_FILE.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
    return build_download_control_payload()


def regenerate_download_control_candidates() -> dict:
    cfg = read_json(CONFIG_FILE, {})
    cfg.update({
        'dry_run': True,
        'skip_watched': False,
        'limit_airing_subjects_to_latest': False,
        'max_download_subjects_per_run': int(cfg.get('control_subject_limit') or 30),
        'max_new_cloud_tasks_per_run': int(cfg.get('control_candidate_limit') or 500),
    })
    cfg.pop('download_subject_ids', None)
    cfg.pop('approved_download_targets', None)
    tmp = ROOT / 'state/download-control-config.tmp.json'
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
    proc = subprocess.run(['/opt/guangya-webdav/venv/bin/python', str(ROOT / 'scripts/bangumi_cloud_download.py'), '--config', str(tmp), '--dry-run'], cwd=str(ROOT), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(proc.stdout[-2000:])
    raw = proc.stdout
    start = raw.rfind('\n{')
    if start >= 0:
        start += 1
    else:
        start = raw.find('{')
    end = raw.rfind('}')
    if start < 0 or end < start:
        raise RuntimeError('dry-run 没有返回 JSON')
    summary = json.loads(raw[start:end+1])
    grouped = {}
    for item in summary.get('submitted') or []:
        sid = str(item.get('subject_id') or '')
        target = f"{str(item.get('target_dir') or '').strip('/')}/{str(item.get('target_name') or '').strip('/')}".strip('/')
        if not sid or not target:
            continue
        g = grouped.setdefault(sid, {
            'subject_id': sid,
            'show': item.get('show') or item.get('display') or '-',
            'collection': normalize_collection(item.get('category_dir') or item.get('collection') or '-'),
            'category_dir': item.get('category_dir') or '',
            'show_dir': item.get('show_dir') or item.get('show') or '',
            'items': [],
        })
        g['items'].append({'title': item.get('source_title') or item.get('title') or '', 'target': target, 'target_name': item.get('target_name') or target.rsplit('/',1)[-1], 'torrent_url': item.get('torrent_url') or ''})
    plan = {'created_at': datetime.now().astimezone().isoformat(), 'status': 'candidate_cache', 'subjects': list(grouped.values()), 'summary': {'subject_count': len(grouped), 'file_count': sum(len(x['items']) for x in grouped.values())}}
    CONTROL_CANDIDATES_FILE.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
    return build_download_control_payload()


@app.route('/download-control')
def download_control_page():
    return render_template_string(CONTROL_HTML)


@app.route('/api/download_control')
def api_download_control():
    return jsonify(build_download_control_payload())


@app.route('/api/download_control/plan', methods=['POST'])
def api_download_control_plan():
    try:
        body = request.get_json(force=True) or {}
        data = save_download_control_plan(body.get('targets') or [], str(body.get('status') or 'pending_confirmation'))
        return jsonify({'ok': True, 'data': data})
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400


@app.route('/api/download_control/regenerate', methods=['POST'])
def api_download_control_regenerate():
    try:
        data = regenerate_download_control_candidates()
        return jsonify({'ok': True, 'data': data})
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
