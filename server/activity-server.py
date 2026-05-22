#!/usr/bin/env python3
"""Activity Browser — Production Flask Server

API v1:
  GET  /api/v1/state       — rhythm + constraints
  GET  /api/v1/filters     — filter definitions (extensible, data-driven)
  GET  /api/v1/activities  — all activities with full data (client-filterable)
  GET  /api/v1/query?f=... — server-filtered results with count
  POST /api/v1/log         — log completed activity
  GET  /api/v1/stats       — recency stats
  POST /api/v1/surprise    — random weighted pick

Port: 8092. systemd: activity-browser.service
"""

import json
import math
import os
import random
import re
import sqlite3
import sys
import time
from datetime import datetime

from flask import Flask, jsonify, request, send_file

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from browse_core import resolve_rhythm
from browse_core import get_level_data as _tree_data  # kept for compat

app = Flask(__name__)
CACHE_DB = os.path.expanduser("~/.hermes/data/activity-cache.db")
HTML_PATH = os.path.join(os.path.dirname(__file__), "..", "static", "index.html")

# ── Filter Config (extensible — add entries here, zero frontend changes) ──
FILTERS_CONFIG = [
    {
        "id": "time", "label": "How much time?", "type": "single",
        "options": [
            {"value": 15, "label": "15m"}, {"value": 30, "label": "30m"},
            {"value": 60, "label": "1hr"}, {"value": 120, "label": "2hr"},
            {"value": 240, "label": "4hr+"},
        ],
        "default": None, "rhythm_default": None,
    },
    {
        "id": "energy", "label": "Energy level?", "type": "single",
        "options": [
            {"value": "drained", "label": "Drained"}, {"value": "low", "label": "Low"},
            {"value": "medium", "label": "Medium"}, {"value": "high", "label": "Energetic"},
        ],
        "default": None, "rhythm_default": "energy",
    },
    {
        "id": "mental", "label": "Mental state?", "type": "single",
        "options": [
            {"value": "fried", "label": "Fried"}, {"value": "tired", "label": "Tired"},
            {"value": "medium", "label": "Okay"}, {"value": "fresh", "label": "Fresh"},
        ],
        "default": None, "rhythm_default": "mental",
    },
    {
        "id": "location", "label": "Where are you?", "type": "single",
        "options": [
            {"value": "home", "label": "🏠 Home"}, {"value": "anywhere", "label": "🌍 Anywhere"},
            {"value": "outdoor", "label": "🌿 Outdoor"},
        ],
        "default": "home", "rhythm_default": None,
    },
    {
        "id": "intent", "label": "What kind?", "type": "multi",
        "options": [
            {"value": "make", "label": "🛠️ Make"}, {"value": "fix", "label": "🏠 Fix"},
            {"value": "move", "label": "🏃 Move"}, {"value": "learn", "label": "🧠 Learn"},
            {"value": "relax", "label": "😌 Relax"},
        ],
        "default": [], "rhythm_default": None,
    },
]

# ── Intent/Category mappings (server-side for query filtering) ──
INTENT_MAP = {"creative":"make","code":"make","making":"make","flying":"make","culinary":"make",
              "household":"fix","garden":"fix","admin":"fix","physical":"move","learning":"learn",
              "relaxation":"relax","entertainment":"relax","social":"move"}
WORK_TYPE_INTENT = {"llm-coding":"make","cad-3d":"make","hardware-build":"make",
                    "sysadmin-config":"fix","research-planning":"learn","documentation":"make"}
ACTIVITY_INTENT_OVERRIDES = {
    "FPV Flying":"move","Fixed Wing Flying":"move","Sim Practice":"learn",
    "Cooking and Baking":"make","Meal Prep":"fix","Foraging":"move",
    "Mushroom Cultivation":"make","Coffee and Tea Brewing":"relax",
    "Server Maintenance":"fix","Workout":"move","Rock Climbing":"move",
    "Hiking":"move","EBike Ride":"move","Running":"move","Yoga and Stretching":"move",
    "Farmers Market":"move","Museum Visit":"learn","Restaurant Outing":"relax","Coffee Walk":"relax",
}

def _get_intent(row):
    name = row["name"]
    if name in ACTIVITY_INTENT_OVERRIDES:
        return ACTIVITY_INTENT_OVERRIDES[name]
    if row["is_project"] and row["work_type"]:
        return WORK_TYPE_INTENT.get(row["work_type"], "make")
    return INTENT_MAP.get(row["category"], "make")

# ── Database ──
def get_conn():
    conn = sqlite3.connect(CACHE_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def get_current_state():
    now = datetime.now()
    day = now.strftime("%a")
    hour = now.hour
    energy, mental, is_work, is_dinner, is_late = resolve_rhythm(day, hour)
    return {"day": day, "hour": hour, "energy": energy, "mental": mental,
            "is_work": is_work, "is_dinner": is_dinner, "is_late": is_late}

# ── API v1 ──

@app.route("/")
def index():
    return send_file(HTML_PATH)

@app.route("/api/v1/state")
def api_state():
    return jsonify(get_current_state())

@app.route("/api/v1/filters")
def api_filters():
    """Return filter definitions. Frontend builds UI from this — zero code changes to add filters."""
    state = get_current_state()
    filters = []
    for f in FILTERS_CONFIG:
        fd = dict(f)
        # Apply rhythm defaults
        if fd.get("rhythm_default") == "energy":
            fd["default"] = state["energy"]
        elif fd.get("rhythm_default") == "mental":
            fd["default"] = state["mental"]
        filters.append(fd)
    return jsonify({"filters": filters, "state": state})

@app.route("/api/v1/activities")
def api_activities():
    """Return all activities with intent/recency metadata for client-side filtering."""
    conn = get_conn()
    rows = conn.execute("SELECT * FROM activities").fetchall()

    # Pre-fetch recency
    names = [r["name"] for r in rows]
    placeholders = ",".join("?" * len(names))
    recency_rows = conn.execute(
        f"SELECT activity_name, MAX(done_at), COUNT(*) FROM recency_log WHERE activity_name IN ({placeholders}) GROUP BY activity_name",
        names).fetchall() if names else []
    recency_map = {r[0]: (r[1], r[2]) for r in recency_rows}

    activities = []
    for r in rows:
        rd = recency_map.get(r["name"], (None, 0))
        last_str = rd[0]
        try:
            days = (datetime.now().date() - datetime.fromisoformat(last_str).date()).days if last_str else None
        except:
            days = None
        activities.append({
            "name": r["name"], "category": r["category"], "subcategory": r["subcategory"],
            "intent": _get_intent(r),
            "min_time": r["min_time"], "max_time": r["max_time"],
            "location": r["location"], "equipment": r["equipment"],
            "noise": r["noise"], "flow": r["flow"], "screen": r["screen"],
            "cost": r["cost"], "setup_time": r["setup_time"], "teardown_time": r["teardown_time"],
            "interrupt": r["interrupt"], "social": r["social"],
            "create_consume": r["create_consume"],
            "recency_ideal": r["recency_ideal"], "base_interest": r["base_interest"],
            "is_project": bool(r["is_project"]), "is_chore": bool(r["is_chore"]),
            "work_type": r["work_type"], "project_name": r["project_name"],
            "next_action": r["next_action"],
            "last_done": last_str, "times_done": rd[1],
            "days_since": days,
            "tags": _build_tags(dict(r)),
        })
    conn.close()
    return jsonify({"activities": activities, "total": len(activities)})

def _build_tags(a):
    tags = []
    if a.get("is_project"): tags.append("project")
    if a.get("is_chore"): tags.append("chore")
    if a.get("weather") == "required": tags.append("needs-weather")
    if a.get("location") in ("trail", "range", "specific-location"): tags.append("outdoor")
    if a.get("flow") == "deep": tags.append("deep-focus")
    if a.get("equipment") in ("specialized", "full-kit"): tags.append("needs-gear")
    if a.get("interrupt") == "no": tags.append("no-interruptions")
    if a.get("screen") == "yes": tags.append("screen")
    return tags

@app.route("/api/v1/query")
def api_query():
    """Server-side filter: ?time=30&energy=low&intent=make,relax"""
    conn = get_conn()
    rows = conn.execute("SELECT * FROM activities").fetchall()
    names = [r["name"] for r in rows]
    placeholders = ",".join("?" * len(names))
    recency_rows = conn.execute(
        f"SELECT activity_name, MAX(done_at), COUNT(*) FROM recency_log WHERE activity_name IN ({placeholders}) GROUP BY activity_name",
        names).fetchall() if names else []
    recency_map = {r[0]: (r[1], r[2]) for r in recency_rows}

    results = []
    for r in rows:
        a = dict(r)
        a["_intent"] = _get_intent(r)
        a["_tags"] = _build_tags(a)
        rd = recency_map.get(a["name"], (None, 0))
        a["last_done"] = rd[0]; a["times_done"] = rd[1]

        # Apply filters from query string
        if not _passes_filters(a, request.args):
            continue
        results.append({
            "name": a["name"], "intent": a["_intent"], "category": a["category"],
            "min_time": a["min_time"], "max_time": a["max_time"],
            "tags": a["_tags"], "days_since": (
                (datetime.now().date() - datetime.fromisoformat(a["last_done"]).date()).days
                if a["last_done"] else None
            ),
            "base_interest": a.get("base_interest", 5),
        })
    conn.close()
    return jsonify({"results": results, "total": len(results), "matched": len(results)})

def _passes_filters(activity, args):
    # Time: activity's max_time should be within 1.5x of available
    t = args.get("time", type=int)
    if t:
        max_t = activity.get("max_time") or 240
        if max_t > t * 1.5:
            return False

    # Location
    loc = args.get("location")
    if loc:
        tags = activity.get("_tags", [])
        if loc == "home" and "outdoor" in tags:
            return False
        if loc == "outdoor" and "outdoor" not in tags:
            return False

    # Intent (comma-separated for multi)
    intent = args.get("intent", "")
    if intent:
        allowed = set(intent.split(","))
        if activity.get("_intent") not in allowed:
            return False

    return True

@app.route("/api/v1/log", methods=["POST"])
def api_log():
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    duration = data.get("duration")
    if not name: return jsonify({"error": "name required"}), 400
    conn = get_conn()
    conn.execute("INSERT INTO recency_log (activity_name, duration_minutes) VALUES (?,?)",(name,duration))
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM recency_log WHERE activity_name=?",(name,)).fetchone()[0]
    conn.close()
    return jsonify({"ok": True, "name": name, "times_done": count})

@app.route("/api/v1/stats")
def api_stats():
    conn = get_conn()
    days = request.args.get("days", 7, type=int)
    recent = conn.execute("""SELECT activity_name, COUNT(*) as cnt, MAX(done_at) as last FROM recency_log WHERE done_at>=date('now',?) GROUP BY activity_name ORDER BY cnt DESC LIMIT 10""",(f"-{days} days",)).fetchall()
    neglected = conn.execute("""SELECT a.name, a.category, a.recency_ideal, COALESCE((SELECT CAST(julianday('now')-julianday(MAX(done_at)) AS INTEGER) FROM recency_log WHERE activity_name=a.name),999) as days_since FROM activities a WHERE a.is_chore=0 ORDER BY days_since DESC LIMIT 8""").fetchall()
    conn.close()
    return jsonify({"recent":[{"name":r[0],"count":r[1],"last":r[2]}for r in recent],"neglected":[{"name":r[0],"category":r[1],"ideal":r[2],"days":r[3]}for r in neglected]})

@app.route("/api/v1/surprise", methods=["POST"])
def api_surprise():
    data = request.get_json() or {}
    scope = data.get("scope", "all")
    conn = get_conn()
    rows = conn.execute("SELECT * FROM activities").fetchall()
    pool = []
    for r in rows:
        a = dict(r); a["_intent"] = _get_intent(r)
        if scope.startswith("intent:") and a["_intent"] != scope.split(":",1)[1]: continue
        if scope.startswith("category:") and a["category"] != scope.split(":",1)[1]: continue
        pool.append(a)
    if not pool: conn.close(); return jsonify(None)
    # Weight by interest
    weights = [(a.get("base_interest") or 5) for a in pool]
    pick = random.choices(pool, weights=[max(w,0.5) for w in weights], k=1)[0]
    conn.close()
    return jsonify({"name":pick["name"],"category":pick["category"],"min_time":pick.get("min_time"),"max_time":pick.get("max_time"),"tags":_build_tags(pick)})

# ── Main ──
def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8092)
    p.add_argument("--host", default="0.0.0.0")
    args = p.parse_args()
    if not os.path.exists(HTML_PATH):
        print(f"ERROR: {HTML_PATH} not found"); sys.exit(1)
    print(f"Activity Browser → http://localhost:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)

if __name__ == "__main__":
    main()
