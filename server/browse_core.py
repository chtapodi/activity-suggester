#!/usr/bin/env python3
"""browse_core.py — Stateless query engine for the interactive activity browser.

Provides get_level_data(), surprise(), get_activity_detail().
Queries the SQLite cache. Self-contained (stdlib only).
"""

import math
import random
import re
import sqlite3
from datetime import date, datetime
import os as _os
import sys as _sys
_scripts_dir = _os.path.dirname(_os.path.abspath(__file__))
if _scripts_dir not in _sys.path:
    _sys.path.insert(0, _scripts_dir)
from activity_rhythm import SCHEDULE, LATE_NIGHT_START, LATE_NIGHT_BLOCK_TAGS, LATE_NIGHT_BLOCK_CATEGORIES, LATE_NIGHT_SCREEN_PENALTY, DINNER_WINDOW, WORKDAY_SHORT_WINDOW, WORKDAY_BLOCK_HOURS

# ── Constants (mirrored from activity-suggest.py) ──────────────────
SEASONS = {3:"spring",4:"spring",5:"spring",6:"summer",7:"summer",8:"summer",
           9:"fall",10:"fall",11:"fall",12:"winter",1:"winter",2:"winter"}
SEASON_MONTHS = {"spring":[3,4,5],"summer":[6,7,8],"fall":[9,10,11],"winter":[12,1,2],
                 "frost-free":[3,4,5,6,7,8,9,10,11],"spring-fall":[3,4,5,9,10,11]}
TIME_OF_DAY = {"early-morning":(4,8),"morning":(8,12),"afternoon":(12,17),
               "evening":(17,21),"night":(21,24),"late-night":(0,4)}
DECAY_RATES = {"daily":0.30,"2-3x-week":0.15,"weekly":0.08,"biweekly":0.04,
               "monthly":0.02,"seasonal":0.005}

# ── Intent mappings ────────────────────────────────────────────────
INTENT_MAP = {"creative":"make","code":"make","making":"make","flying":"make",
              "culinary":"make","household":"fix","garden":"fix","admin":"fix",
              "physical":"move","learning":"learn","relaxation":"relax",
              "entertainment":"relax","social":"move"}

ACTIVITY_INTENT_OVERRIDES = {"FPV Flying":"move","Fixed Wing Flying":"move",
    "Sim Practice":"learn","Cooking and Baking":"make","Meal Prep":"fix",
    "Foraging":"move","Mushroom Cultivation":"make","Coffee and Tea Brewing":"relax",
    "Server Maintenance":"fix","Workout":"move","Rock Climbing":"move",
    "Hiking":"move","EBike Ride":"move","Running":"move","Yoga and Stretching":"move",
    "Farmers Market":"move","Museum Visit":"learn","Restaurant Outing":"relax",
    "Coffee Walk":"relax"}

WORK_TYPE_INTENT = {"llm-coding":"make","cad-3d":"make","hardware-build":"make",
                    "sysadmin-config":"fix","research-planning":"learn",
                    "documentation":"make"}

INTENT_LABELS = {"make":"🛠️  Make something","fix":"🏠  Fix something",
                 "move":"🏃  Move my body","learn":"🧠  Feed my brain",
                 "relax":"😌  Just relax"}

CATEGORY_LABELS = {"code":"💻 Code projects","making":"🔧 Making & 3D printing",
    "creative":"🎨 Creative & crafting","culinary":"🍳 Cooking & food",
    "flying":"✈️  Flying & RC","household":"🧹 Household chores",
    "garden":"🌱 Garden","admin":"📋 Admin & planning",
    "physical":"🏋️  Physical","learning":"📚 Learning","relaxation":"🧘 Relaxation",
    "entertainment":"📺 Entertainment","social":"👥 Social & outings"}

SAFETY_VALVE = {"Meditation","Napping","Music Listening","Reading (Light)",
                "Yoga and Stretching","Browsing Online"}


# ── Rhythm Resolution ───────────────────────────────────────────────
def resolve_rhythm(day_of_week, hour, explicit_energy=None, explicit_mental=None):
    """Return (energy, mental, is_work_hours, is_dinner, is_late_night) from schedule."""
    day_type = "weekend" if day_of_week in ("Sat", "Sun") else "weekday"
    energy = explicit_energy
    mental = explicit_mental

    # Find matching schedule entry
    for pat, (h_start, h_end), e, m, _note in SCHEDULE:
        if pat in (day_type, "any"):
            if h_start <= hour < h_end:
                if energy is None:
                    energy = e
                if mental is None:
                    mental = m
                break

    if energy is None:
        energy = "medium"
    if mental is None:
        mental = "medium"

    # Work hours check
    is_work_hours = False
    if day_type == "weekday":
        for ws, we in WORKDAY_BLOCK_HOURS:
            if ws <= hour < we:
                is_work_hours = True
                break

    is_dinner = day_type == "weekday" and DINNER_WINDOW[0] <= hour < DINNER_WINDOW[1]
    is_late_night = hour >= LATE_NIGHT_START or hour < 5

    return energy, mental, is_work_hours, is_dinner, is_late_night

# ── Gate Logic ─────────────────────────────────────────────────────
def _get_tod(hour):
    for tod,(s,e) in TIME_OF_DAY.items():
        if s<e and s<=hour<e: return tod
        if s>e and (hour>=s or hour<e): return tod
    return "any"

def _check_hour_window(window, hour):
    if window in ("any",None,""): return True
    wm = {"morning":range(4,12),"afternoon":range(12,17),"daytime":range(6,20),
          "evening":range(17,21),"night":[21,22,23,0,1,2,3],
          "evening-night":range(17,24),"after-dark":range(20,24),"dawn-dusk":range(6,20)}
    w = str(window)
    parts = w.split("-")
    if len(parts)==2 and parts[0] in wm and parts[1] in wm:
        return hour in (set(wm[parts[0]])|set(wm[parts[1]]))
    if w in wm: return hour in set(wm[w])
    m = re.match(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", w)
    if m:
        sh,_,eh,_ = map(int,m.groups())
        return sh<=hour<eh if sh<=eh else (hour>=sh or hour<eh)
    return True

def _check_day(day_spec, day_name):
    if day_spec in ("any",None,""): return True
    if day_spec=="weekend" and day_name in ("Sat","Sun"): return True
    if day_spec=="weekday" and day_name in ("Mon","Tue","Wed","Thu","Fri"): return True
    m = re.match(r"(\w+)-(\w+)", str(day_spec))
    if m:
        days = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
        try:
            si,ei,ci = days.index(m.group(1)),days.index(m.group(2)),days.index(day_name)
            return si<=ci<=ei if si<=ei else (ci>=si or ci<=ei)
        except ValueError: return True
    return day_name==day_spec

def _check_season(season_spec, month):
    if season_spec in ("all-year",None,""): return True
    return month in SEASON_MONTHS.get(season_spec,[])

def apply_gates(row, c):
    """Return True if activity passes all hard gates (including rhythm constraints)."""
    # Time gate
    if c["time_available"] < (row["min_time"] or 0): return False

    # Work hours: only short activities (≤30min) during core work hours
    if c.get("is_work_hours") and (row["min_time"] or 0) > WORKDAY_SHORT_WINDOW:
        return False

    # Dinner window: no activities
    if c.get("is_dinner"):
        return False

    # Late night: block deep-focus, gear-heavy, outdoor, physical, social
    if c.get("is_late_night"):
        tags = _build_tags(dict(row))
        if any(t in tags for t in LATE_NIGHT_BLOCK_TAGS):
            return False
        if row["category"] in LATE_NIGHT_BLOCK_CATEGORIES:
            return False
        if row["flow"] == "deep":
            return False
    loc = row["location"] or "anywhere"
    if c["at_home"] and loc in ("nearby-outdoors","trail","specific-location","range"): return False
    if not c["at_home"] and loc=="home": return False
    if not _check_day(row["day"], c["day_of_week"]): return False
    if not _check_hour_window(row["hour_window"], c["current_hour"]): return False
    if not _check_season(row["season"], c["current_month"]): return False
    if row["weather"]=="required" and c["weather"]!="good": return False
    wind = row["weather_wind"] or "any"
    if wind=="calm" and c["wind"]!="calm": return False
    if wind=="moderate-max" and c["wind"]=="strong": return False
    if row["noise"]=="loud" and _get_tod(c["current_hour"]) in ("night","late-night"): return False
    if row["noise"]=="moderate" and _get_tod(c["current_hour"])=="late-night": return False
    return True

# ── Intent resolution ──────────────────────────────────────────────
def _get_intent(row):
    """Determine the intent for an activity row."""
    name = row["name"]
    if name in ACTIVITY_INTENT_OVERRIDES:
        return ACTIVITY_INTENT_OVERRIDES[name]
    if row["is_project"] and row.get("work_type"):
        return WORK_TYPE_INTENT.get(row["work_type"], "make")
    return INTENT_MAP.get(row["category"], "make")

# ── Recency pre-fetch ──────────────────────────────────────────────
def _prefetch_recency(conn, names):
    """Return {name: (last_done_str, count)} for given activity names."""
    if not names: return {}
    placeholders = ",".join("?"*len(names))
    rows = conn.execute(f"""
        SELECT activity_name, MAX(done_at), COUNT(*)
        FROM recency_log WHERE activity_name IN ({placeholders})
        GROUP BY activity_name
    """, list(names)).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}

def _format_recency(last_done_str, count):
    if not last_done_str: return "never done", "🆕"
    try:
        days = (date.today()-datetime.fromisoformat(last_done_str).date()).days
    except: return "never done", "🆕"
    if days==0: return "today", "✅"
    if days==1: return "yesterday", "✅"
    if days<=3: return f"{days} days ago", "✅"
    if days<=7: return f"{days} days ago", "⏰"
    if days<=30: return f"{days//7}w ago", "⏰"
    return f"{days//30}mo ago", "⚠️"

# ── Core API ───────────────────────────────────────────────────────
def get_level_data(conn, constraints, scope="all"):
    """Return LevelData dict for the given scope under constraints."""
    c = constraints
    rows = conn.execute("SELECT * FROM activities").fetchall()

    # Apply gates
    passing = [dict(r) for r in rows if apply_gates(r, c)]

    if not passing:
        return _safety_valve_data(conn)

    if scope == "all":
        return _build_intent_level(passing)
    elif scope.startswith("intent:"):
        intent = scope.split(":",1)[1]
        return _build_category_or_activity_level(passing, intent)
    elif scope.startswith("category:"):
        category = scope.split(":",1)[1]
        return _build_activity_level(passing, category, conn)
    return _safety_valve_data(conn)

def _safety_valve_data(conn):
    rows = conn.execute("SELECT * FROM activities WHERE name IN ({})".format(
        ",".join("?"*len(SAFETY_VALVE))), list(SAFETY_VALVE)).fetchall()
    groups = []
    for r in rows:
        groups.append({"key":r["name"],"label":r["name"],"count":1,
            "min_time":r["min_time"],"max_time":r["max_time"],
            "category":r["category"],"recency":"","tags":[]})
    return {"level_name":"safety_valve","title":"Nothing fits — always available",
            "groups":groups,"total_available":len(groups)}

def _build_intent_level(passing):
    intents = {}
    for a in passing:
        intent = _get_intent(a)
        intents.setdefault(intent,[]).append(a)

    groups = []
    for intent in ["make","fix","move","learn","relax"]:
        if intent not in intents: continue
        groups.append({"key":intent,"label":INTENT_LABELS.get(intent,intent),
            "count":len(intents[intent]),"representative":None,
            "subcategory":None,"min_time":None,"max_time":None,
            "recency":None,"tags":[]})
    return {"level_name":"intent","title":"What kind of thing?",
            "groups":groups,"total_available":len(passing)}

def _build_category_or_activity_level(passing, intent):
    mine = [a for a in passing if _get_intent(a)==intent]
    if not mine:
        return {"level_name":"activity","title":"Nothing available",
                "groups":[],"total_available":0}

    # Should we skip category level?
    # Map project work_types back to display categories
    WORK_TYPE_TO_CATEGORY = {"llm-coding":"code","cad-3d":"making",
        "hardware-build":"making","sysadmin-config":"code",
        "research-planning":"learning","documentation":"code"}
    categories = {}
    for a in mine:
        cat = a["category"]
        if a.get("is_project") and cat in WORK_TYPE_TO_CATEGORY:
            cat = WORK_TYPE_TO_CATEGORY[cat]
        categories.setdefault(cat,[]).append(a)

    if len(mine) <= 5 or len(categories) <= 1:
        # Skip to activity list
        return _build_activity_list(mine, INTENT_LABELS.get(intent,intent), None)

    # Build category menu
    groups = []
    for cat in sorted(categories):
        groups.append({"key":cat,"label":CATEGORY_LABELS.get(cat,cat),
            "count":len(categories[cat]),"representative":None,
            "subcategory":None,"min_time":None,"max_time":None,
            "recency":None,"tags":[]})
    return {"level_name":"category",
            "title":f"{INTENT_LABELS.get(intent,intent)}  ·  {len(mine)} available",
            "groups":groups,"total_available":len(mine)}

def _build_activity_level(passing, category, conn):
    WORK_TYPE_TO_CATEGORY = {"llm-coding":"code","cad-3d":"making",
        "hardware-build":"making","sysadmin-config":"code",
        "research-planning":"learning","documentation":"code"}
    mine = []
    for a in passing:
        cat = a["category"]
        if a.get("is_project") and cat in WORK_TYPE_TO_CATEGORY:
            cat = WORK_TYPE_TO_CATEGORY[cat]
        if cat == category:
            mine.append(a)
    return _build_activity_list(mine, CATEGORY_LABELS.get(category,category), conn)

def _build_activity_list(activities, title, conn):
    # Pre-fetch recency
    recency = {}
    if conn:
        names = [a["name"] for a in activities]
        recency = _prefetch_recency(conn, names)

    groups = []
    for a in activities:
        rd = recency.get(a["name"], (None,0))
        last_str, count = rd
        last_label, emoji = _format_recency(last_str, count)
        groups.append({"key":a["name"],"label":a["name"],"count":1,
            "representative":a,"subcategory":a.get("subcategory",""),
            "min_time":a["min_time"],"max_time":a["max_time"],
            "category":a["category"],
            "recency":f"{emoji} {last_label}" if last_str else f"{emoji} {last_label}",
            "tags":_build_tags(a)})
    return {"level_name":"activity","title":f"{title}  ·  {len(activities)} available",
            "groups":groups,"total_available":len(activities)}

def _build_tags(a):
    tags = []
    if a.get("is_project"): tags.append("project")
    if a.get("is_chore"): tags.append("chore")
    if a.get("weather")=="required": tags.append("needs-weather")
    if a.get("location") in ("trail","range","specific-location"): tags.append("outdoor")
    if a.get("flow")=="deep": tags.append("deep-focus")
    if a.get("equipment") in ("specialized","full-kit"): tags.append("needs-gear")
    if a.get("interrupt")=="no": tags.append("no-interruptions")
    return tags

# ── Surprise ───────────────────────────────────────────────────────
def surprise(conn, constraints, scope="all", exclude=None):
    """Pick one activity from scope, weighted by interest, excluding seen."""
    exclude = exclude or set()
    c = constraints
    rows = conn.execute("SELECT * FROM activities").fetchall()
    passing = [dict(r) for r in rows if apply_gates(r, c)]

    if scope.startswith("intent:"):
        intent = scope.split(":",1)[1]
        passing = [a for a in passing if _get_intent(a)==intent]
    elif scope.startswith("category:"):
        cat = scope.split(":",1)[1]
        passing = [a for a in passing if a["category"]==cat]

    eligible = [a for a in passing if a["name"] not in exclude]
    if not eligible: return None

    # Pre-fetch recency for weighting
    names = [a["name"] for a in eligible]
    recency = _prefetch_recency(conn, names)

    weights = []
    for a in eligible:
        base = a.get("base_interest",5) or 5
        rd = recency.get(a["name"],(None,0))
        if rd[0]:
            try: days = (date.today()-datetime.fromisoformat(rd[0]).date()).days
            except: days = 0
        else: days = 0
        w = base * math.exp(-0.05 * max(days,0))
        weights.append(max(w, 0.5))

    pick = random.choices(eligible, weights=weights, k=1)[0]
    rd = recency.get(pick["name"],(None,0))
    last_str, emoji = _format_recency(rd[0], rd[1])
    pick["_recency"] = f"{emoji} {last_str}" if rd[0] else f"{emoji} {last_str}"
    return pick

# ── Activity Detail ────────────────────────────────────────────────
def get_activity_detail(conn, name):
    """Return full activity data with recency and hints."""
    row = conn.execute("SELECT * FROM activities WHERE name=?",(name,)).fetchone()
    if not row: return None
    a = dict(row)
    rd = _prefetch_recency(conn, [name])
    if name in rd:
        last_str, count = rd[name]
        last_label, emoji = _format_recency(last_str, count)
        a["_recency"] = f"{emoji} {last_label}"
        a["_times_done"] = count
    a["_tags"] = _build_tags(a)
    a["_intent"] = _get_intent(a)
    return a
