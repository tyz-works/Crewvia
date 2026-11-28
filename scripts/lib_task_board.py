#!/usr/bin/env python3
"""止まっている所と依存の図を 1 枚の自己完結 HTML にする (board.html)。

**純粋関数だけ**。ファイルも queue も開かず、依存の「満たされた」も判定しない。
入力は plan.sh が `lib_task_cards` / `lib_dep_rules` から作った dict で、ここは
並べて描くだけ (設計: knowledge/task-graph.md「board.html」)。

入力の形 (`missions`):

    [{'slug': str,
      'tasks': [{'id': str, 'title': str, 'status': str, 'worker': str|None,
                 'started_at': str|None, 'reason': str|None,
                 'deps': [id...],      # 宣言された依存 (図の矢印)
                 'held': [id...],      # failed のため保留になっている依存 (lib_dep_rules の答え)
                 'unmet': [id...],     # まだ満たされていない依存 (同上)
                 'unreadable': bool,   # card が読めない
                 'blocks': [id...]}]}] # この task (止まっている) が塞いでいる下流 (lib_dep_rules の答え)

出す文字列は title / status / 依存 / Worker / 時刻 / 理由の 1 行まで。Result・本文・env は
入力に含まれない。**文字列はすべて HTML エスケープ**し、外部の CSS / JS / フォントは読まない。
"""

from __future__ import annotations

import datetime
import html
import re
import sys
from pathlib import Path

# status の語彙は lib_task_status.py が唯一の置き場。plan.sh は spec で読むので隣の lib を通す。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib_task_status import (  # noqa: E402
    AWAITING_DECISION_STATUSES, EXECUTING_STATUSES, HELD_DEP_STATUSES, TERMINAL_STATUSES,
)

#: in_progress がこの分数を超えたら「長い」。ページ内の JS も同じ値で数え続ける。
LONG_RUNNING_MINUTES = 60
#: 自分を読み直す間隔 (秒)。
REFRESH_SECONDS = 5

#: 止まっている所の種類 → (見出し, 色クラス)。並びがそのまま画面の並び。
STUCK_KINDS = [
    ('needs_director', '要判断 (needs_director)'),
    ('failed', '失敗 (failed)'),
    ('held', '依存先が failed で保留 (出口: plan.sh release-dep)'),
    ('blocked', '停止 (blocked)'),
    ('unreadable', '読めない card'),
    ('long', f'in_progress のまま {LONG_RUNNING_MINUTES} 分以上'),
]

# 判断待ち (TASK_GRAPH_STATUS_MAP の「要判断」と同じ範囲) / 失敗 / 終端 / 実行中。集合は lib_task_status から。
_NEEDS_DIRECTOR = AWAITING_DECISION_STATUSES | {'ready_for_verification'}
_FAILED = frozenset(HELD_DEP_STATUSES) | {'verification_failed'}
_DONE = TERMINAL_STATUSES
_RUNNING = EXECUTING_STATUSES

_CTRL = re.compile(r'[\x00-\x1f\x7f-\x9f  ]')
REASON_LIMIT = 200
TITLE_LIMIT = 120

NODE_W, NODE_H = 210, 74
GAP_X, GAP_Y = 70, 18


def _text(value, limit=None):
    """画面に出す 1 行の文字列。制御文字は空白にし、長ければ切る (エスケープは出す側)。"""
    s = _CTRL.sub(' ', '' if value is None else str(value)).strip()
    if limit and len(s) > limit:
        s = s[:limit - 1] + '…'
    return s


def _esc(value, limit=None):
    return html.escape(_text(value, limit), quote=True)


def parse_epoch(value):
    """ISO 8601 の時刻 → epoch 秒。読めなければ None (「長い」とは言わない側)。"""
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()  # plan.sh の now_iso() は naive のローカル時刻
    return dt.timestamp()


def static_stuck_kind(task):
    """時間に依らず止まっている task の種類 (STUCK_KINDS のキー) / None。

    `held` の判定は呼び出し側が `lib_dep_rules` から渡した値を読むだけ。
    """
    if task.get('unreadable'):
        return 'unreadable'
    status = task.get('status')
    if status in _NEEDS_DIRECTOR:
        return 'needs_director'
    if status in _FAILED:
        return 'failed'
    if status == 'blocked':
        return 'blocked'
    if status == 'pending' and task.get('held'):
        return 'held'
    return None


def is_long_running(task, now_epoch):
    if task.get('status') not in _RUNNING or task.get('unreadable'):
        return False
    started = parse_epoch(task.get('started_at'))
    return started is not None and now_epoch - started >= LONG_RUNNING_MINUTES * 60


def _box_class(task):
    if task.get('unreadable'):
        return 'unreadable'
    status = task.get('status')
    if status in _DONE:
        return 'done'
    if status in _RUNNING:
        return 'running'
    if status in _FAILED:
        return 'failed'
    if status in _NEEDS_DIRECTOR or status == 'blocked':
        return 'blocked'
    if status == 'pending':
        return 'held' if task.get('held') else ('waiting' if task.get('unmet') else 'ready')
    return 'unknown'


def _layout(tasks):
    """依存の深さで列を決める (描画用。「満たされた」の判定ではない)。循環・未定義の依存でも終わる。"""
    ids = [t['id'] for t in tasks]
    known = set(ids)
    depth = {i: 0 for i in ids}
    deps = {t['id']: [d for d in t.get('deps', []) if d in known and d != t['id']] for t in tasks}
    for _ in range(len(ids)):
        changed = False
        for i in ids:
            for d in deps[i]:
                if depth[i] < depth[d] + 1 and depth[d] + 1 < len(ids):
                    depth[i] = depth[d] + 1
                    changed = True
        if not changed:
            break
    rows = {}
    pos = {}
    for i in ids:  # 入力の並び (tNNN 順) を保つ
        r = rows.get(depth[i], 0)
        rows[depth[i]] = r + 1
        pos[i] = (depth[i], r)
    return pos, deps, (max(depth.values()) + 1 if ids else 0), (max(rows.values()) if rows else 0)


def _render_node(task, slug, x, y, blocked_by_stuck):
    cls = _box_class(task)
    started = task.get('started_at') if task.get('status') in _RUNNING else None
    el = f' <span class="el" data-since="{_esc(started)}"></span>' if started else ''
    worker = f'<span class="w">{_esc(task.get("worker"))}</span>' if task.get('worker') else ''
    extra = ''
    if blocked_by_stuck:
        extra = ' blocked-down'
    title = _text(task.get('title'), TITLE_LIMIT)
    tip = f'{task["id"]} {title} [{task.get("status")}]'
    return (
        f'<div class="node {cls}{extra}" id="n-{_esc(slug)}-{_esc(task["id"])}" '
        f'style="left:{x}px;top:{y}px" title="{html.escape(_text(tip), quote=True)}">'
        f'<div class="h"><b>{_esc(task["id"])}</b> <span class="st">{_esc(task.get("status"))}</span>{el}</div>'
        f'<div class="t">{html.escape(title, quote=True)}</div>'
        f'<div class="f">{worker}</div></div>'
    )


def _render_mission(mission, stuck_ids_down):
    slug = mission['slug']
    tasks = mission['tasks']
    out = [f'<section class="mission"><h3>{_esc(slug)}</h3>']
    if not tasks:
        out.append('<p class="none">task がありません</p></section>')
        return ''.join(out)
    pos, deps, ncols, nrows = _layout(tasks)
    width = ncols * (NODE_W + GAP_X) - GAP_X
    height = nrows * (NODE_H + GAP_Y) - GAP_Y

    def xy(i):
        c, r = pos[i]
        return c * (NODE_W + GAP_X), r * (NODE_H + GAP_Y)

    lines = []
    for t in tasks:
        tx, ty = xy(t['id'])
        for d in deps[t['id']]:
            dx, dy = xy(d)
            x1, y1, x2, y2 = dx + NODE_W, dy + NODE_H // 2, tx, ty + NODE_H // 2
            mid = (x1 + x2) / 2
            cls = 'edge held' if d in (t.get('held') or []) else 'edge'
            lines.append(f'<path class="{cls}" d="M{x1},{y1} C{mid},{y1} {mid},{y2} {x2},{y2}" '
                         f'marker-end="url(#arr)"/>')
    nodes = [_render_node(t, slug, *xy(t['id']), (slug, t['id']) in stuck_ids_down) for t in tasks]
    out.append(
        f'<div class="graph" style="width:{width}px;height:{height}px">'
        f'<svg width="{width}" height="{height}">'
        '<defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
        'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#8b949e"/></marker></defs>'
        + ''.join(lines) + '</svg>' + ''.join(nodes) + '</div></section>')
    return ''.join(out)


_CSS = """
body{font:14px/1.5 system-ui,sans-serif;background:#0d1117;color:#e6edf3;margin:0;padding:16px}
h1{font-size:18px;margin:0 0 4px}h2{font-size:15px;margin:18px 0 6px}h3{font-size:14px;margin:14px 0 6px;color:#9ecbff}
.gen{color:#8b949e;font-size:12px}
.stuck{border:1px solid #30363d;border-radius:6px;padding:8px 12px;background:#161b22}
.stuck.ok{border-color:#238636;color:#7ee787}.stuck.bad{border-color:#da3633}
.stuck h4{margin:8px 0 2px;font-size:13px;color:#ffa657}.stuck ul{margin:0 0 4px;padding-left:20px}
.stuck li{margin:2px 0}.reason{color:#8b949e}.long-item{display:none}.long-item.on{display:list-item}
.graph{position:relative;margin:6px 0 12px;overflow:visible}
.graph svg{position:absolute;left:0;top:0}
.edge{fill:none;stroke:#6e7681;stroke-width:1.5}.edge.held{stroke:#f85149;stroke-dasharray:5 3}
.node{position:absolute;box-sizing:border-box;width:210px;height:74px;padding:4px 7px;border-radius:6px;
border:2px solid #30363d;background:#161b22;overflow:hidden;font-size:12px}
.node .h{white-space:nowrap}.node .st{color:#8b949e}.node .t{line-height:1.25;max-height:2.5em;overflow:hidden}
.node .w{color:#d2a8ff}.node .f{position:absolute;bottom:3px;left:7px}.node .el{color:#79c0ff}
.node.done{border-color:#238636;opacity:.7}.node.running{border-color:#388bfd}
.node.ready{border-color:#d29922}.node.waiting{border-color:#6e7681}
.node.blocked,.node.failed,.node.held{border-color:#f85149;background:#2a1517}
.node.unreadable,.node.unknown{border-color:#a371f7;background:#20182e}
.node.blocked-down{box-shadow:0 0 0 2px #f8514966;background:#2a1f17}
.legend{color:#8b949e;font-size:12px;margin-top:8px}.none{color:#8b949e}
"""

_JS = """
(function(){
var LONG=%d*60;
function fmt(s){s=Math.max(0,Math.floor(s));var h=Math.floor(s/3600),m=Math.floor(s%%3600/60);
return h>0?h+'h'+('0'+m).slice(-2)+'m':m+'m'+('0'+(s%%60)).slice(-2)+'s';}
function tick(){
 var now=Date.now()/1000;
 document.querySelectorAll('.el').forEach(function(e){
  var t=Date.parse(e.getAttribute('data-since'));
  e.textContent=isNaN(t)?'':'⏱'+fmt(now-t/1000);});
 var shown=0;
 document.querySelectorAll('.long-item').forEach(function(e){
  var t=Date.parse(e.getAttribute('data-since')),on=!isNaN(t)&&now-t/1000>=LONG;
  e.classList.toggle('on',on);if(on)shown++;
  var el=e.querySelector('.el');if(el)el.textContent=isNaN(t)?'':'⏱'+fmt(now-t/1000);});
 var g=document.getElementById('long-group');if(g)g.style.display=shown?'block':'none';
 var c=document.getElementById('stuck-count'),box=document.getElementById('stuck-box');
 var total=parseInt(box.getAttribute('data-static'),10)+shown;
 c.textContent=total;
 box.className='stuck '+(total?'bad':'ok');
 document.getElementById('none-msg').style.display=total?'none':'block';
}
tick();setInterval(tick,1000);
})();
""" % LONG_RUNNING_MINUTES


def render_board(missions, generated_at, now_epoch, unreadable_note=None):
    """board.html の全文 (str)。`generated_at` は表示用の ISO 文字列、`now_epoch` は生成時の時刻。"""
    groups = {k: [] for k, _ in STUCK_KINDS}
    stuck_down = set()
    for m in missions:
        for t in m['tasks']:
            kind = static_stuck_kind(t)
            if kind:
                groups[kind].append((m['slug'], t))
                for b in t.get('blocks') or []:
                    stuck_down.add((m['slug'], b))
            elif is_long_running(t, now_epoch):
                groups['long'].append((m['slug'], t))
            elif t.get('status') in _RUNNING:
                groups['long'].append((m['slug'], t))  # 今は短い: JS が閾値を超えたら出す

    def item(slug, t, cls=''):
        reason = f' <span class="reason">— {_esc(t["reason"], REASON_LIMIT)}</span>' if t.get('reason') else ''
        held = (f' <span class="reason">(保留の原因: {_esc(", ".join(t["held"]))})</span>'
                if t.get('held') else '')
        down = ''
        if t.get('blocks'):
            down = f' <span class="reason">→ 塞いでいる下流: {_esc(", ".join(t["blocks"]))}</span>'
        who = f' [{_esc(t["worker"])}]' if t.get('worker') else ''
        since = ''
        if cls:
            since = f' data-since="{_esc(t.get("started_at"))}"'
            down += ' <span class="el"></span>'
        return (f'<li class="{cls}"{since}><b>{_esc(slug)}/{_esc(t["id"])}</b>{who} '
                f'{_esc(t.get("title"), TITLE_LIMIT)} <span class="reason">[{_esc(t.get("status"))}]</span>'
                f'{reason}{held}{down}</li>')

    static_count = 0
    parts = []
    for kind, label in STUCK_KINDS:
        entries = groups[kind]
        if kind == 'long':
            lis = ''.join(item(s, t, 'long-item' + (' on' if is_long_running(t, now_epoch) else ''))
                          for s, t in entries)
            if lis:
                shown = any(is_long_running(t, now_epoch) for _, t in entries)
                parts.append(f'<div id="long-group" style="display:{"block" if shown else "none"}">'
                             f'<h4>{_esc(label)}</h4><ul>{lis}</ul></div>')
            continue
        if not entries:
            continue
        static_count += len(entries)
        parts.append(f'<h4>{_esc(label)} ({len(entries)})</h4><ul>'
                     + ''.join(item(s, t) for s, t in entries) + '</ul>')
    initial = static_count + sum(1 for _, t in groups['long'] if is_long_running(t, now_epoch))
    box_cls = 'bad' if initial else 'ok'
    note = f'<p class="gen">{_esc(unreadable_note)}</p>' if unreadable_note else ''
    body = [
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">',
        f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<title>crewvia board</title>',
        f'<style>{_CSS}</style></head><body>',
        '<h1>crewvia board</h1>',
        f'<div class="gen">生成: {_esc(generated_at)} · {REFRESH_SECONDS} 秒ごとに自動で読み直し</div>', note,
        '<h2>止まっている所 (<span id="stuck-count">' + str(initial) + '</span>)</h2>',
        f'<div class="stuck {box_cls}" id="stuck-box" data-static="{static_count}">',
        f'<div id="none-msg" style="display:{"none" if initial else "block"}">止まっている所はありません</div>',
        ''.join(parts), '</div>',
        '<h2>依存の図</h2>',
    ]
    if not missions:
        body.append('<p class="none">active な mission がありません</p>')
    for m in missions:
        body.append(_render_mission(m, stuck_down))
    body.append('<div class="legend">矢印: 依存先 → 依存元 · 赤の破線: failed のため保留 · '
                '橙の影: 止まっている task に塞がれている下流</div>')
    body.append(f'<script>{_JS}</script></body></html>\n')
    return '\n'.join(body)
