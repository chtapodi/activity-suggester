#!/usr/bin/env python3
"""Generate a standalone HTML flowchart browser for the activity suggester."""

import json
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from browse_core import get_level_data, INTENT_LABELS, CATEGORY_LABELS
from browse_core import resolve_rhythm
from datetime import datetime

CACHE_DB = os.path.expanduser("~/.hermes/data/activity-cache.db")
OUTPUT_PATH = os.path.expanduser("~/activity-browser.html")

def get_tree_data():
    os.makedirs(os.path.dirname(CACHE_DB), exist_ok=True)
    conn = sqlite3.connect(CACHE_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # If cache is empty, rebuild via the CLI
    try:
        cnt = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    except:
        cnt = 0
    if cnt == 0:
        import subprocess
        subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "activity-suggest.py"), "rebuild"],
                       capture_output=True, timeout=30)

    now = datetime.now()
    day = now.strftime("%a")
    hour = now.hour
    energy, mental, is_work, is_dinner, is_late = resolve_rhythm(day, hour)

    constraints = {
        "time_available": 120,
        "physical_energy": energy,
        "mental_freshness": mental,
        "at_home": True,
        "weather": "good",
        "wind": "calm",
        "temperature": None,
        "social": "either",
        "day_of_week": day,
        "current_hour": hour,
        "current_month": now.month,
        "is_work_hours": is_work,
        "is_dinner": is_dinner,
        "is_late_night": is_late,
    }

    # Build the full tree: intents → categories → activities
    intent_data = get_level_data(conn, constraints, "all")
    tree = {"intents": [], "state": {"day": day, "hour": hour, "energy": energy, "mental": mental,
              "is_work": is_work, "is_dinner": is_dinner, "is_late": is_late}}

    for g in intent_data.get("groups", []):
        intent_key = g["key"]
        intent_node = {"key": intent_key, "label": g["label"], "count": g["count"], "categories": []}

        cat_data = get_level_data(conn, constraints, f"intent:{intent_key}")
        for cg in cat_data.get("groups", []):
            cat_key = cg["key"]
            cat_node = {"key": cat_key, "label": cg["label"], "count": cg["count"], "activities": []}

            act_data = get_level_data(conn, constraints, f"category:{cat_key}")
            for ag in act_data.get("groups", []):
                cat_node["activities"].append({
                    "name": ag["label"],
                    "min_time": ag.get("min_time"),
                    "max_time": ag.get("max_time"),
                    "recency": ag.get("recency", ""),
                    "tags": ag.get("tags", []),
                    "category": ag.get("category", ""),
                })
            intent_node["categories"].append(cat_node)
        tree["intents"].append(intent_node)

    conn.close()
    return tree


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Activity Browser</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{
  background:#0d0d1a;
  color:#c8b896;
  font-family:'Georgia','Times New Roman',serif;
  min-height:100vh;
  overflow-x:hidden;
}
body::before{
  content:'';position:fixed;top:0;left:0;width:100%;height:100%;
  background:radial-gradient(ellipse at 20% 50%,#1a1520 0%,transparent 50%),
             radial-gradient(ellipse at 80% 20%,#1a1a2e 0%,transparent 50%),
             radial-gradient(ellipse at 50% 80%,#162030 0%,transparent 50%);
  opacity:0.6;pointer-events:none;z-index:0;
}
.container{position:relative;z-index:1;max-width:1200px;margin:0 auto;padding:20px}

/* Header */
.header{
  text-align:center;padding:30px 20px 10px;
  border-bottom:1px solid #3a3020;
  margin-bottom:20px;
}
.header h1{
  font-size:2em;color:#d4a853;font-weight:normal;
  letter-spacing:3px;text-transform:uppercase;
  text-shadow:0 0 30px rgba(212,168,83,0.3);
}
.header .state{
  font-size:0.85em;color:#8a7a5a;margin-top:8px;
  font-style:italic;
}
.header .state span{color:#b8955a;margin:0 6px}

/* Flow area */
.flow{display:flex;flex-direction:column;align-items:center;gap:0;position:relative}
.level-label{
  color:#6a5a3a;font-size:0.75em;text-transform:uppercase;
  letter-spacing:4px;margin:20px 0 8px;
}

/* Node rows */
.node-row{
  display:flex;flex-wrap:wrap;justify-content:center;
  gap:16px;margin:8px 0;
}

/* Connector lines */
.connector{
  width:2px;height:30px;background:linear-gradient(to bottom,#4a3a20,transparent);
  margin:0 auto;
}

/* Card nodes */
.card{
  background:linear-gradient(135deg,#1a1410,#1e1814);
  border:1px solid #3a3020;
  border-radius:2px;
  padding:18px 22px;
  cursor:pointer;
  transition:all 0.25s ease;
  position:relative;
  min-width:150px;
  max-width:220px;
  text-align:center;
  box-shadow:0 2px 12px rgba(0,0,0,0.4),inset 0 1px 0 rgba(255,255,255,0.02);
}
.card::before{
  content:'';position:absolute;top:3px;left:3px;right:3px;bottom:3px;
  border:1px solid rgba(180,150,100,0.08);border-radius:1px;pointer-events:none;
}
.card:hover{
  border-color:#8a6a30;transform:translateY(-2px);
  box-shadow:0 6px 24px rgba(0,0,0,0.5),0 0 20px rgba(180,140,60,0.1),inset 0 1px 0 rgba(255,255,255,0.03);
}
.card:active{transform:scale(0.97)}
.card .icon{font-size:1.6em;display:block;margin-bottom:6px}
.card .name{font-size:0.95em;color:#c8b080;display:block}
.card .count{font-size:0.7em;color:#6a5a3a;margin-top:4px}
.card.selected{
  border-color:#d4a853;
  box-shadow:0 0 30px rgba(212,168,83,0.2),inset 0 0 20px rgba(212,168,83,0.05);
}

/* Activity cards (smaller, list-style) */
.activity-card{
  background:linear-gradient(135deg,#141210,#181614);
  border:1px solid #2a2218;
  padding:12px 16px;min-width:130px;max-width:200px;
  font-size:0.85em;text-align:left;
}
.activity-card .act-name{color:#b8955a;font-size:0.85em;margin-bottom:4px}
.activity-card .act-meta{font-size:0.7em;color:#5a4a30}
.activity-card .act-recency{font-size:0.7em;color:#7a6a4a;margin-top:3px}
.activity-card:hover{border-color:#6a5020}

/* Back button */
.back-row{text-align:center;margin:16px 0}
.back-btn{
  background:none;border:1px solid #3a3020;color:#8a7a5a;
  padding:8px 24px;cursor:pointer;font-family:inherit;font-size:0.85em;
  transition:all 0.2s;border-radius:2px;
}
.back-btn:hover{border-color:#8a6a30;color:#c8b080}

/* Selected activity detail */
.detail-panel{
  background:linear-gradient(135deg,#141210,#1a1612);
  border:2px solid #4a3820;border-radius:3px;
  padding:30px;max-width:500px;margin:20px auto;
  text-align:center;box-shadow:0 4px 30px rgba(0,0,0,0.5);
}
.detail-panel h2{color:#d4a853;font-size:1.4em;font-weight:normal;margin-bottom:10px}
.detail-panel .meta{color:#8a7a5a;font-size:0.85em;margin:6px 0}
.detail-panel .tags{margin:10px 0}
.detail-panel .tag{
  display:inline-block;background:rgba(180,150,100,0.1);border:1px solid #3a3020;
  padding:3px 10px;margin:3px;font-size:0.75em;border-radius:2px;color:#8a7a5a;
}
.detail-actions{margin-top:16px;display:flex;gap:10px;justify-content:center;flex-wrap:wrap}
.detail-actions button{
  background:rgba(180,150,100,0.1);border:1px solid #3a3020;color:#c8b896;
  padding:8px 20px;cursor:pointer;font-family:inherit;font-size:0.85em;
  transition:all 0.2s;border-radius:2px;
}
.detail-actions button:hover{border-color:#8a6a30;color:#d4a853}
.detail-actions button.primary{background:rgba(180,140,60,0.15);border-color:#5a4020}

/* Logged toast */
.toast{
  position:fixed;bottom:30px;left:50%;transform:translateX(-50%);
  background:#1a3020;border:1px solid #3a5a30;color:#8ab880;
  padding:12px 28px;border-radius:3px;font-size:0.9em;
  opacity:0;transition:opacity 0.3s;z-index:10;
}
.toast.show{opacity:1}

/* Surprise button */
.surprise-btn{
  background:linear-gradient(135deg,rgba(180,140,60,0.1),rgba(180,140,60,0.05));
  border:1px dashed #5a4020;color:#b8955a;
  padding:10px 28px;cursor:pointer;font-family:inherit;font-size:0.9em;
  transition:all 0.2s;border-radius:2px;margin:10px;
}
.surprise-btn:hover{border-color:#d4a853;color:#d4a853;box-shadow:0 0 20px rgba(212,168,83,0.1)}

/* Scroll hint */
.scroll-hint{text-align:center;color:#4a3a20;font-size:0.75em;margin:10px 0;font-style:italic}

/* Responsive */
@media(max-width:600px){
  .header h1{font-size:1.4em}
  .card{padding:12px 14px;min-width:120px;max-width:160px}
  .card .name{font-size:0.8em}
}
</style>
</head>
<body>
<div class="container">
<div class="header">
  <h1>⚜ What Shall You Do ⚜</h1>
  <div class="state" id="stateBar"></div>
</div>

<div class="flow" id="flowArea"></div>
<div class="toast" id="toast"></div>
</div>

<script>
const TREE = __TREE_DATA__;

const ICONS = {
  make:'🛠️',fix:'🏠',move:'🏃',learn:'🧠',relax:'😌',
  code:'💻',making:'🔧',creative:'🎨',culinary:'🍳',flying:'✈️',
  household:'🧹',garden:'🌱',admin:'📋',physical:'🏋️',learning:'📚',
  relaxation:'🧘',entertainment:'📺',social:'👥',
  'llm-coding':'💻','cad-3d':'📐','hardware-build':'🔧',
  'sysadmin-config':'⚙️','research-planning':'🔍','documentation':'📝'
};

let path = [];
let currentView = 'intents';

function init(){
  const s = TREE.state;
  let notes = [];
  if(s.is_work) notes.push('work hours');
  if(s.is_dinner) notes.push('dinner time');
  if(s.is_late) notes.push('late night');
  document.getElementById('stateBar').innerHTML =
    `${s.day} ${s.hour}:00 <span>·</span> energy:${s.energy} <span>·</span> mental:${s.mental}` +
    (notes.length ? ` <span>·</span> ${notes.join(' · ')}` : '');
  renderIntents();
}

function renderIntents(){
  path = [];
  currentView = 'intents';
  let html = '<div class="level-label">What kind of thing?</div><div class="node-row">';
  TREE.intents.forEach((g,i) => {
    html += `<div class="card" onclick="pickIntent('${g.key}')">
      <span class="icon">${ICONS[g.key]||'•'}</span>
      <span class="name">${g.label.replace(/^.[^ ]+ /,'')}</span>
      <span class="count">${g.count} available</span>
    </div>`;
  });
  if(!acts.length){
    html += '<div class="detail-panel" style="margin-top:20px"><p>Nothing available in this category right now.</p><p style="color:#6a5a3a;font-size:0.8em">Try a different category or time of day.</p></div>';
  }
  html += '</div><div class="back-row">';
  html += '<button class="surprise-btn" onclick="surpriseMe()">🎲 Surprise Me</button>';
  html += '</div>';
  document.getElementById('flowArea').innerHTML = html;
}

function pickIntent(key){
  const intent = TREE.intents.find(i=>i.key===key);
  if(!intent) return;
  if(intent.categories.length===0){renderActivities(intent.key,null,intent.label);return}
  path = [key];
  currentView = 'categories';
  let html = '<div class="connector"></div>';
  let cleanIntent = intent.label.replace(/^.[^ ]+ /,'');
  html += `<div class="level-label">${cleanIntent}</div><div class="node-row">`;
  intent.categories.forEach(c => {
    html += `<div class="card" onclick="pickCategory('${c.key}')">
      <span class="icon">${ICONS[c.key]||'📂'}</span>
      <span class="name">${c.label.replace(/^.[^ ]+ /,'')}</span>
      <span class="count">${c.count} items</span>
    </div>`;
  });
  if(!acts.length){
    html += '<div class="detail-panel" style="margin-top:20px"><p>Nothing available in this category right now.</p><p style="color:#6a5a3a;font-size:0.8em">Try a different category or time of day.</p></div>';
  }
  html += '</div><div class="back-row">';
  html += `<button class="back-btn" onclick="renderIntents()">← Back to intents</button>`;
  html += '<button class="surprise-btn" onclick="surpriseMe()">🎲 Surprise</button>';
  html += '</div>';
  document.getElementById('flowArea').innerHTML = html;
  document.getElementById('flowArea').scrollIntoView({behavior:'smooth'});
}

function pickCategory(key){
  const intent = TREE.intents.find(i=>i.key===path[0]);
  if(!intent) return;
  const cat = intent.categories.find(c=>c.key===key);
  if(!cat) return;
  renderActivities(intent.key, key, cat.label);
}

function renderActivities(intentKey, catKey, title){
  let acts;
  if(catKey){
    const intent = TREE.intents.find(i=>i.key===intentKey);
    const cat = intent.categories.find(c=>c.key===catKey);
    acts = cat?cat.activities:[];
    path = [intentKey, catKey];
  } else {
    acts = TREE.intents.find(i=>i.key===intentKey)?.categories.flatMap(c=>c.activities)||[];
    path = [intentKey];
  }
  currentView = 'activities';

  let html = '<div class="connector"></div>';
  let cleanTitle = title.replace(/^.[^ ]+ /,'');
  html += `<div class="level-label">${cleanTitle} · ${acts.length} options</div><div class="node-row">`;
  acts.forEach((a,i) => {
    const t = a.min_time && a.max_time ? `${a.min_time}–${a.max_time}m` : '';
    html += `<div class="card activity-card" onclick="showDetail('${escapeHtml(a.name)}','${escapeHtml(t)}','${escapeHtml(a.recency||'')}','${escapeHtml(a.category)}','${escapeHtml((a.tags||[]).join(','))}')">
      <div class="act-name">${escapeHtml(a.name)}</div>
      <div class="act-meta">${t}</div>
      <div class="act-recency">${escapeHtml(a.recency||'')}</div>
    </div>`;
  });
  if(!acts.length){
    html += '<div class="detail-panel" style="margin-top:20px"><p>Nothing available in this category right now.</p><p style="color:#6a5a3a;font-size:0.8em">Try a different category or time of day.</p></div>';
  }
  html += '</div><div class="back-row">';
  if(path.length>1){
    html += `<button class="back-btn" onclick="pickIntent('${path[0]}')">← Back to categories</button>`;
  } else {
    html += `<button class="back-btn" onclick="renderIntents()">← Back to intents</button>`;
  }
  html += '<button class="surprise-btn" onclick="surpriseMe()">🎲 Surprise</button>';
  html += '</div>';
  document.getElementById('flowArea').innerHTML = html;
  document.getElementById('flowArea').scrollIntoView({behavior:'smooth'});
}

function showDetail(name, time, recency, category, tagsStr){
  currentView = 'detail';
  const tags = tagsStr?tagsStr.split(','):[];
  let html = '<div class="connector"></div>';
  html += `<div class="detail-panel">
    <h2>${name}</h2>
    <div class="meta">${time}</div>
    <div class="meta">${recency}</div>
    <div class="meta">${category}</div>`;
  if(tags.length && tags[0]){
    html += '<div class="tags">';
    tags.forEach(t => {html += `<span class="tag">${t}</span>`});
    html += '</div>';
  }
  html += `<div class="detail-actions">
    <button class="primary" onclick="logActivity('${escapeHtml(name)}')">✓ Log It</button>
    <button onclick="surpriseMe()">🎲 Something Else</button>
    <button class="back-btn" onclick="goBackFromDetail()">← Back</button>
  </div></div>`;
  document.getElementById('flowArea').innerHTML = html;
}

function goBackFromDetail(){
  if(path.length===2) pickCategory(path[1]);
  else if(path.length===1) pickIntent(path[0]);
  else renderIntents();
}

function surpriseMe(){
  let pool;
  if(path.length===2){
    const intent = TREE.intents.find(i=>i.key===path[0]);
    const cat = intent?.categories.find(c=>c.key===path[1]);
    pool = cat?cat.activities:[];
  } else if(path.length===1){
    const intent = TREE.intents.find(i=>i.key===path[0]);
    pool = intent?intent.categories.flatMap(c=>c.activities):[];
  } else {
    pool = TREE.intents.flatMap(i=>i.categories.flatMap(c=>c.activities));
  }
  if(!pool.length) return;
  const pick = pool[Math.floor(Math.random()*pool.length)];
  showDetail(pick.name, pick.min_time&&pick.max_time?`${pick.min_time}–${pick.max_time}m`:'',
             pick.recency||'', pick.category, (pick.tags||[]).join(','));
}

function logActivity(name){
  fetch('/log',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:name})
  }).then(r=>r.json()).then(d=>{
    showToast(`✓ Logged: ${name}`);
    setTimeout(()=>renderIntents(),1200);
  }).catch(()=>{
    showToast(`✓ Noted: ${name} (offline — run activity-suggest done --done "${name}")`);
    setTimeout(()=>renderIntents(),2000);
  });
}

function showToast(msg){
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),2500);
}

function escapeHtml(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}

init();
</script>
</body>
</html>"""

def main():
    tree = get_tree_data()
    html = HTML_TEMPLATE.replace("__TREE_DATA__", json.dumps(tree, indent=2))
    with open(OUTPUT_PATH, "w") as f:
        f.write(html)
    print(f"Generated: {OUTPUT_PATH}")
    print(f"  Intents: {len(tree['intents'])}")
    total_acts = sum(len(c["activities"]) for i in tree["intents"] for c in i["categories"])
    print(f"  Activities: {total_acts}")
    print(f"  Open: file://{OUTPUT_PATH}")

if __name__ == "__main__":
    main()
