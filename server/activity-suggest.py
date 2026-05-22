#!/usr/bin/env python3
"""Activity Suggester — SQLite-backed constraint-based activity ranking.

Cache: ~/.hermes/data/activity-cache.db  (rebuilt when vault changes)
Source: Obsidian vault activity notes + project roadmaps

Usage:
  activity-suggest [--time N] [--energy low|medium|high] [...]
  activity-suggest --done "Activity Name" [--duration N]
  activity-suggest --stats [--days N]
  activity-suggest --rebuild
"""

import argparse
import glob
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime

# Browse mode imports
import sys as _sys
_scripts_dir = os.path.dirname(os.path.abspath(__file__))
if _scripts_dir not in _sys.path:
    _sys.path.insert(0, _scripts_dir)
from browse_core import get_level_data, surprise, get_activity_detail, INTENT_LABELS, CATEGORY_LABELS
from browse_core import resolve_rhythm
from activity_rhythm import LATE_NIGHT_SCREEN_PENALTY
from browse_session import BrowseSession

# ── Configuration ──────────────────────────────────────────────────
VAULT_ROOT = os.environ.get("OBSIDIAN_VAULT_PATH", os.path.expanduser("~/workspace/Obsidian"))
ACTIVITIES_GLOB = f"{VAULT_ROOT}/Projects/Areas/Life/Activities/**/*.md"
PROJECTS_GLOB = f"{VAULT_ROOT}/Projects/Areas/Build/**/*.md"
CACHE_DIR = os.path.expanduser("~/.hermes/data")
CACHE_DB = os.path.join(CACHE_DIR, "activity-cache.db")

# ── Constants ──────────────────────────────────────────────────────
SEASONS = {
    3: "spring", 4: "spring", 5: "spring",
    6: "summer", 7: "summer", 8: "summer",
    9: "fall", 10: "fall", 11: "fall",
    12: "winter", 1: "winter", 2: "winter",
}
SEASON_MONTHS = {
    "spring": [3, 4, 5], "summer": [6, 7, 8],
    "fall": [9, 10, 11], "winter": [12, 1, 2],
    "frost-free": [3, 4, 5, 6, 7, 8, 9, 10, 11],
    "spring-fall": [3, 4, 5, 9, 10, 11],
}
DECAY_RATES = {
    "daily": 0.30, "2-3x-week": 0.15, "weekly": 0.08,
    "biweekly": 0.04, "monthly": 0.02, "seasonal": 0.005,
}
TIME_OF_DAY = {
    "early-morning": (4, 8), "morning": (8, 12),
    "afternoon": (12, 17), "evening": (17, 21),
    "night": (21, 24), "late-night": (0, 4),
}
SAFETY_VALVE_NAMES = {"Meditation", "Napping", "Music Listening", "Reading (Light)", "Yoga and Stretching"}

# ── Work Type Classification ────────────────────────────────────────
WORK_TYPE_PATTERNS = {
    "llm-coding": [
        r"\bcode\b", r"\bscript\b", r"\bimplement\b", r"\bclass\b", r"\bfunction\b",
        r"\bfix\b", r"\brefactor\b", r"\bfeature\b", r"\bpipeline\b", r"\bapi\b",
        r"\bendpoint\b", r"\broute\b", r"\bweb\b", r"\bapp\b", r"\bui\b", r"\bux\b",
        r"\bfrontend\b", r"\bbackend\b", r"\bdatabase\b", r"\bschema\b", r"\bmigration\b",
        r"\btest\b", r"\bunit test", r"\bintegration test", r"\be2e\b", r"\bvitest\b",
        r"\bpytest\b", r"\bES6\b", r"\bmodule", r"\brepo\b", r"\bcommit\b",
        r"\bPR\b", r"\bmerge\b", r"\bbranch\b", r"\bCI\b", r"\bCD\b",
        r"\bembedding", r"\bvector\b", r"\bmodel\b", r"\bLLM\b", r"\bprompt\b",
        r"\bscraper?\b", r"\bparser?\b", r"\bautomation\b", r"\bbot\b", r"\btelegram\b",
        r"\bcron\b", r"\bscheduler?\b", r"\btool\b", r"\bcli\b", r"\bcommand\b",
        r"\bjson\b", r"\bcsv\b", r"\bimport\b", r"\bexport\b", r"\bdata\b",
        r"\bHA\b", r"\bHome Assistant\b", r"\bintegration\b", r"\binterface\b",
        r"\bcapture\b", r"\blogging\b",
    ],
    "cad-3d": [
        r"\bCAD\b", r"\b3D\s*print", r"\bprint\b", r"\bmodel\b", r"\bblender\b",
        r"\bdesign\b.*\bpart\b", r"\bSTL\b", r"\bSTEP\b", r"\bfilament\b",
        r"\bslice\b", r"\bfit\b.*\bprinter\b", r"\bbuild volume",
        r"\bOpenSCAD\b", r"\bFreeCAD\b", r"\bmechanical\b",
    ],
    "hardware-build": [
        r"\bsolder\b", r"\bassemble\b", r"\bbuild\b", r"\bmount\b", r"\bwire\b",
        r"\bflash\b", r"\bfirmware\b", r"\bfly\b", r"\btest flight", r"\bEdgeTX\b",
        r"\bBetaFlight\b", r"\btransmitter\b", r"\breceiver\b", r"\bbattery\b",
        r"\bmotor\b", r"\bESC\b", r"\bprop\b", r"\bframe\b", r"\bstand\b",
        r"\bvoltage sensor", r"\bcurrent sensor", r"\bpower supply",
        r"\belectronic", r"\banode\b", r"\bcathode", r"\brust\b",
        r"\bparts\b", r"\baudit\b.*\bparts\b", r"\bacquire\b.*\bmaterials?\b",
    ],
    "sysadmin-config": [
        r"\binstall\b", r"\bapt\b", r"\bpip\b", r"\bnpm\b", r"\bconfigure\b",
        r"\bdocker\b", r"\brclone\b", r"\baws\b", r"\bs3\b", r"\bIAM\b",
        r"\bsetup\b", r"\bprovision\b", r"\bdeploy\b", r"\bbucket\b",
        r"\bbackup\b", r"\brestore\b", r"\bencrypt\b", r"\bcredential",
        r"\bsystemd\b", r"\btimer\b", r"\bservice\b", r"\bcron\b",
        r"\bport\b", r"\bproxy\b", r"\bnetwork\b", r"\bTailscale\b",
        r"\bvpn\b", r"\bssh\b", r"\bcert\b", r"\btls\b", r"\bssl\b",
        r"\bpermission", r"\buser\b", r"\bgroup\b", r"\bchown\b", r"\bchmod\b",
        r"\bupdate\b", r"\bupgrade\b", r"\bpatch\b", r"\bversion\b",
    ],
    "research-planning": [
        r"\bresearch\b", r"\binvestigate\b", r"\bevaluate\b", r"\bcompare\b",
        r"\baudit\b", r"\breview\b", r"\bassess?\b", r"\banaly[sz]e\b",
        r"\bexplore\b", r"\bsurvey\b", r"\bfind\b", r"\bsearch\b",
        r"\bidentify\b", r"\bdetermine\b", r"\bplan\b", r"\bscope\b",
        r"\brequirements?\b", r"\boutline\b", r"\bproposal?\b",
    ],
    "documentation": [
        r"\bdocument\b", r"\bdocs?\b", r"\bREADME\b", r"\bwrite\b.*\bup\b",
        r"\bwiki\b", r"\bnote\b", r"\blog\b", r"\bchangelog\b",
    ],
}
WORK_TYPE_DEFAULTS = {
    "llm-coding": {"phy": "none", "men": "high", "min_time": 60, "max_time": 240,
                   "location": "home", "equipment": "specialized", "noise": "silent",
                   "screen": "yes", "flow": "deep", "setup_time": "none", "teardown_time": "none",
                   "interrupt": "risky"},
    "cad-3d": {"phy": "none", "men": "high", "min_time": 60, "max_time": 240,
               "location": "home", "equipment": "specialized", "noise": "silent",
               "screen": "yes", "flow": "deep", "setup_time": "none", "teardown_time": "none",
               "interrupt": "safe"},
    "hardware-build": {"phy": "light", "men": "medium", "min_time": 60, "max_time": 180,
                       "location": "home", "equipment": "specialized", "noise": "moderate",
                       "screen": "no", "flow": "medium", "setup_time": "moderate", "teardown_time": "moderate",
                       "interrupt": "risky"},
    "sysadmin-config": {"phy": "none", "men": "high", "min_time": 30, "max_time": 180,
                        "location": "home", "equipment": "specialized", "noise": "silent",
                        "screen": "yes", "flow": "deep", "setup_time": "none", "teardown_time": "none",
                        "interrupt": "risky"},
    "research-planning": {"phy": "none", "men": "medium", "min_time": 30, "max_time": 120,
                          "location": "anywhere", "equipment": "basic", "noise": "quiet",
                          "screen": "yes", "flow": "medium", "setup_time": "none", "teardown_time": "none",
                          "interrupt": "safe"},
    "documentation": {"phy": "none", "men": "medium", "min_time": 30, "max_time": 120,
                      "location": "anywhere", "equipment": "basic", "noise": "silent",
                      "screen": "yes", "flow": "medium", "setup_time": "none", "teardown_time": "none",
                      "interrupt": "safe"},
}
WORK_TYPE_LABELS = {
    "llm-coding": "llm", "cad-3d": "cad", "hardware-build": "hw",
    "sysadmin-config": "sys", "research-planning": "rsch", "documentation": "doc",
}

# ── YAML Frontmatter Parser ────────────────────────────────────────
def parse_frontmatter(filepath: str) -> dict | None:
    try:
        with open(filepath) as f:
            content = f.read()
    except (IOError, UnicodeDecodeError):
        return None
    if not content.startswith("---"):
        return None
    end = content.find("---", 3)
    if end == -1:
        return None
    return _parse_simple_yaml(content[3:end].strip())

def _parse_simple_yaml(yaml_str: str) -> dict:
    result = {}
    for line in yaml_str.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if key.startswith("-"):
            continue
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1]
        elif val.startswith("'") and val.endswith("'"):
            val = val[1:-1]
        if val in ("null", ""):
            val = None
        elif val in ("true", "True"):
            val = True
        elif val in ("false", "False"):
            val = False
        elif isinstance(val, str) and val.isdigit():
            val = int(val)
        elif isinstance(val, str) and val.replace(".", "", 1).replace("-", "", 1).isdigit():
            try:
                val = float(val)
            except ValueError:
                pass
        result[key] = val
    return result

# ── Vault Hash ─────────────────────────────────────────────────────
def vault_mtime_hash() -> str:
    """Hash of most recent mtime across all activity and project files."""
    h = hashlib.md5()
    files = sorted(glob.glob(ACTIVITIES_GLOB, recursive=True))
    files += sorted(glob.glob(PROJECTS_GLOB, recursive=True))
    for fp in files:
        if "/Archive/" in fp or "/Implementation/" in fp or "/Plans/" in fp:
            continue
        try:
            h.update(f"{fp}:{os.path.getmtime(fp)}".encode())
        except OSError:
            pass
    return h.hexdigest()

# ── Roadmap Parsing ─────────────────────────────────────────────────
def classify_work_type(text: str) -> str:
    text_lower = text.lower()
    scores = {}
    for wtype, patterns in WORK_TYPE_PATTERNS.items():
        score = sum(1 for p in patterns if re.search(p, text_lower))
        if score > 0:
            scores[wtype] = score
    return max(scores, key=scores.get) if scores else "llm-coding"

def find_roadmap_file(project_dir: str) -> str | None:
    for pat in [os.path.join(project_dir, "*Roadmap*.md"),
                os.path.join(project_dir, "**/*Roadmap*.md")]:
        matches = sorted(glob.glob(pat, recursive=True))
        if matches:
            return matches[0]
    return None

def parse_next_action(project_dir: str) -> dict | None:
    roadmap_path = find_roadmap_file(project_dir)
    if not roadmap_path:
        for fp in sorted(glob.glob(os.path.join(project_dir, "*.md"))):
            if "Roadmap" in fp or "Implementation" in fp:
                continue
            try:
                with open(fp) as f:
                    if re.search(r'^[\s]*[-*]\s*\[\s*\]\s*', f.read(), re.MULTILINE):
                        roadmap_path = fp
                        break
            except (IOError, UnicodeDecodeError):
                continue
    if not roadmap_path:
        return None
    try:
        with open(roadmap_path) as f:
            content = f.read()
    except (IOError, UnicodeDecodeError):
        return None
    m = re.search(r'^[\s]*[-*]\s*\[\s*\]\s*(.+?)$', content, re.MULTILINE)
    if not m:
        return None
    action = m.group(1).strip()
    action = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', action)
    action = re.sub(r'`([^`]+)`', r'\1', action)
    action = re.sub(r'\*\*([^*]+)\*\*', r'\1', action)
    if not action:
        return None
    if len(action) > 100:
        action = action[:97] + "..."
    context = ""
    for line in reversed(content[:m.start()].split("\n")):
        if line.strip().startswith("#"):
            context = line.strip().lstrip("#").strip()
            break
    return {"action_text": action, "work_type": classify_work_type(action), "context_line": context}

# ── SQLite Cache ────────────────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    os.makedirs(CACHE_DIR, exist_ok=True)
    conn = sqlite3.connect(CACHE_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS activities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            category TEXT, subcategory TEXT,
            phy TEXT, men TEXT, min_time INTEGER, max_time INTEGER,
            location TEXT, day TEXT, hour_window TEXT, season TEXT,
            weather TEXT, weather_wind TEXT, weather_temp TEXT,
            equipment TEXT, noise TEXT,
            cost TEXT, setup_time TEXT, teardown_time TEXT,
            interrupt TEXT, screen TEXT, flow TEXT, mess TEXT,
            create_consume TEXT, social TEXT,
            recency_ideal TEXT, streak_benefit INTEGER,
            base_interest REAL, is_project INTEGER, is_chore INTEGER,
            work_type TEXT, project_name TEXT, next_action TEXT,
            vault_path TEXT
        );
        CREATE TABLE IF NOT EXISTS recency_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            activity_name TEXT NOT NULL,
            done_at TEXT NOT NULL DEFAULT (datetime('now')),
            duration_minutes INTEGER,
            FOREIGN KEY (activity_name) REFERENCES activities(name)
        );
        CREATE INDEX IF NOT EXISTS idx_recency_name ON recency_log(activity_name);
        CREATE INDEX IF NOT EXISTS idx_recency_date ON recency_log(done_at);
        CREATE INDEX IF NOT EXISTS idx_activities_category ON activities(category);
    """)
    conn.commit()

def rebuild_cache(conn: sqlite3.Connection, verbose=False):
    """Rebuild cache from vault. Destroys and recreates activity data."""
    start = time.time()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM activities")
    conn.execute("PRAGMA foreign_keys=ON")

    # Load static activities
    count = 0
    seen = set()
    for filepath in sorted(glob.glob(ACTIVITIES_GLOB, recursive=True)):
        if "Locations/" in filepath or "Equipment/" in filepath or "Overview" in filepath:
            continue
        fm = parse_frontmatter(filepath)
        if not fm or fm.get("type") != "activity":
            continue
        name = os.path.splitext(os.path.basename(filepath))[0]
        if name in seen:
            continue
        seen.add(name)
        conn.execute("""INSERT OR REPLACE INTO activities
            (name, category, subcategory, phy, men, min_time, max_time,
             location, day, hour_window, season, weather, weather_wind, weather_temp,
             equipment, noise, cost, setup_time, teardown_time,
             interrupt, screen, flow, mess, create_consume, social,
             recency_ideal, streak_benefit, base_interest, is_project, is_chore,
             work_type, project_name, next_action, vault_path)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            name, fm.get("category"), fm.get("subcategory"),
            fm.get("phy"), fm.get("men"), fm.get("min_time"), fm.get("max_time"),
            fm.get("location"), str(fm.get("day", "any")), str(fm.get("hour_window", "any")),
            fm.get("season"), fm.get("weather"), fm.get("weather_wind"), fm.get("weather_temp"),
            fm.get("equipment"), fm.get("noise"),
            fm.get("cost"), fm.get("setup_time"), fm.get("teardown_time"),
            fm.get("interrupt"), fm.get("screen"), fm.get("flow"), fm.get("mess"),
            fm.get("create_consume"), fm.get("social"),
            fm.get("recency_ideal"), 1 if fm.get("streak_benefit") else 0,
            fm.get("base_interest", 5), 0,
            1 if fm.get("chore_priority") else 0,
            fm.get("category"), None, None, filepath
        ))
        count += 1

    # Load projects
    project_dirs = {}
    for filepath in sorted(glob.glob(PROJECTS_GLOB, recursive=True)):
        if "/Archive/" in filepath or "/Implementation/" in filepath or "/Plans/" in filepath:
            continue
        fm = parse_frontmatter(filepath)
        if not fm or fm.get("type") not in ("project",):
            continue
        if fm.get("status") not in ("active", "wip"):
            continue
        name = fm.get("project") or os.path.splitext(os.path.basename(filepath))[0]
        for sfx in [" - Overview", " - Architecture", " - Roadmap", " - Requirements",
                    " - Design", " - Plan", " - Research Plan",
                    " - Data Pipeline Architecture", " - Geospatial Engine Architecture",
                    " - Audit", " - Audit - Flavor Graph Audit"]:
            if name.endswith(sfx):
                name = name[:-len(sfx)]
                break
        if not name:
            continue
        if name not in project_dirs:
            project_dirs[name] = {"path": filepath, "wip": False}
        project_dirs[name]["wip"] = project_dirs[name]["wip"] or (fm.get("status") == "wip")

    pcount = 0
    for pname, pinfo in project_dirs.items():
        pdir = os.path.dirname(pinfo["path"])
        na = parse_next_action(pdir)
        wt = na["work_type"] if na else "llm-coding"
        d = WORK_TYPE_DEFAULTS.get(wt, WORK_TYPE_DEFAULTS["llm-coding"])
        activity_name = na["action_text"] if na else f"Work on {pname}"
        if activity_name in seen:
            continue
        seen.add(activity_name)
        conn.execute("""INSERT OR REPLACE INTO activities
            (name, category, subcategory, phy, men, min_time, max_time,
             location, day, hour_window, season, weather, weather_wind, weather_temp,
             equipment, noise, cost, setup_time, teardown_time,
             interrupt, screen, flow, mess, create_consume, social,
             recency_ideal, streak_benefit, base_interest, is_project, is_chore,
             work_type, project_name, next_action, vault_path)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            activity_name, wt, "project",
            d["phy"], d["men"], d["min_time"], d["max_time"],
            d["location"], "any", "any", "all-year", "no", "any", "any",
            d["equipment"], d["noise"],
            "free", d["setup_time"], d["teardown_time"],
            d["interrupt"], d["screen"], d["flow"], "none",
            "create", "solo",
            "weekly", 1, 8 if na else 7,
            1, 0, wt, pname,
            na["action_text"] if na else None,
            pinfo["path"]
        ))
        pcount += 1

    # Recency is tracked via recency_log table — queried at scoring time

    # Load tasks from Active Work and Life domain notes
    tcount = _discover_tasks(conn, seen)

    # Store hash
    h = vault_mtime_hash()
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('vault_hash', ?)", (h,))
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_rebuild', ?)",
                 (datetime.now().isoformat(),))
    conn.commit()

    elapsed = time.time() - start
    if verbose:
        total = count + pcount + tcount
        print(f"Rebuilt cache: {count} activities + {pcount} projects + {tcount} tasks = {total} total in {elapsed*1000:.0f}ms")


# ── Task Discovery ──────────────────────────────────────────────────
TASK_SOURCES = [
    (f"{VAULT_ROOT}/Projects/Hub/Active Work.md", "household"),
    (f"{VAULT_ROOT}/Projects/Areas/Build/Making/Crafting/Things to make.md", "making"),
]
TASK_SEARCH_DIRS = [
    (f"{VAULT_ROOT}/Projects/Areas/Life/Garden", "garden"),
]
# Personal tasks — only scan specific known task files, not the whole domain
PERSONAL_TASK_FILES = [
    f"{VAULT_ROOT}/Projects/Areas/Life/Personal/Health/Doctor notes.md",
    f"{VAULT_ROOT}/Projects/Areas/Life/Personal/Health/Personal - Health - Doctor Notes.md",
    f"{VAULT_ROOT}/Projects/Areas/Life/Personal/Clothing/To buy.md",
    f"{VAULT_ROOT}/Projects/Areas/Life/Personal/Clothing/Personal - Clothing - To Buy.md",
    f"{VAULT_ROOT}/Projects/Areas/Life/Personal/Personal.md",
    f"{VAULT_ROOT}/Projects/Areas/Life/Personal/Personal - Overview.md",
]

def _discover_tasks(conn, seen_names: set) -> int:
    """Scan vault for actionable - [ ] tasks, inject as chore activities."""
    tasks = {}  # text → (category, filepath)

    # 1. Specific known task files — with section awareness
    for filepath, default_cat in TASK_SOURCES:
        try:
            with open(filepath) as f:
                raw = f.read()
            # Parse sections to skip code/HA/automation tasks
            current_section = ""
            skip_sections = {"home automation", "server", "code", "code & ai"}
            for line in raw.split("\n"):
                if line.startswith("## "):
                    current_section = line[3:].strip().lower()
                m = re.match(r'^[\s]*-[\s]*\[[\s]*\][\s]*(.+?)$', line)
                if not m:
                    continue
                # Skip technical sections
                if current_section in skip_sections:
                    continue
                text = m.group(1).strip()
                text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
                text = re.sub(r'`([^`]+)`', r'\1', text)
                text = re.sub(r'🔥|🔁|⏫|⏬|🔽|⚠️|✅|📝', '', text).strip()
                # Strip remaining wikilinks
                text = re.sub(r'\[\[([^\]|]+)\]\]', r'\1', text)
                text = re.sub(r'\[\[([^\]]+)\|([^\]]+)\]\]', r'\2', text)
                text = re.sub(r'\s*—\s*`[^`]+`.*$', '', text)
                text = re.sub(r'\(due:.*?\)', '', text).strip()
                if not text or len(text) < 5 or len(text) > 100:
                    continue
                if text.lower().startswith(("http", "see ", "note:", "todo:", "- [")):
                    continue
                tasks[text] = (default_cat, filepath)
        except (IOError, UnicodeDecodeError):
            pass

    # 2. Life domain directories
    for search_dir, default_cat in TASK_SEARCH_DIRS:
        for filepath in sorted(glob.glob(f"{search_dir}/**/*.md", recursive=True)):
            if "Activities/" in filepath or "Archive/" in filepath:
                continue
            try:
                with open(filepath) as f:
                    raw = f.read()
                for m in re.finditer(r'^[\s]*-[\s]*\[[\s]*\][\s]*(.+?)$', raw, re.MULTILINE):
                    text = m.group(1).strip()
                    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'', text)
                    text = re.sub(r'`([^`]+)`', r'', text)
                    text = re.sub(r'🔥|🔁|⏫|⏬|🔽|⚠️|✅|📝', '', text).strip()
                    text = re.sub(r'\[\[([^\]|]+)\]\]', r'', text)
                    text = re.sub(r'\[\[([^\]]+)\|([^\]]+)\]\]', r'', text)
                    if not text or len(text) < 5 or len(text) > 100:
                        continue
                    if text in tasks:
                        continue
                    if text.lower().startswith(("http", "see ", "note:", "todo:", "- [", "add ", "(add")):
                        continue
                    # Skip generic one-word template tasks
                    if text.lower() in ("design outline", "determine materials", "acquire materials",
                                        "implement", "iterate", "test", "design", "requirements",
                                        "scope", "steps required", "design implementation plan"):
                        continue
                    if any(skip in text.lower() for skip in (
                        "pyscript", "evalfunc", "docker", "compose", "api",
                        "endpoint", "schema", "migration", "database",
                        "css", "html", "javascript", "react", "vue")):
                        continue
                    tasks[text] = (default_cat, filepath)
            except (IOError, UnicodeDecodeError):
                pass
                pass

    # 2b. Personal task files (specific allowlist, action-filtered)
    action_patterns = [
        r'\bcall\b', r'\bbuy\b', r'\border\b', r'\bget\b', r'\bmake\b.*\bappointment',
        r'\bschedule\b', r'\bremind\b', r'\bcheck\b', r'\bfollow\b.*\bup',
        r'\bpick\b.*\bup', r'\bdrop\b.*\boff', r'\breturn\b',
        r'\binsurance\b', r'\bprescription\b', r'\bpharmacy\b', r'\bdentist\b',
        r'\bdoctor\b', r'\btest\b', r'\bblood\b', r'\bx-ray\b',
        r'\bpair\b', r'\bundershirt\b', r'\bshirt\b', r'\bshorts?\b', r'\bchinos?\b',
        r'\bregister\b', r'\bsign\b.*\bup', r'\bapply\b', r'\bsubmit\b',
        r'\brenew\b', r'\bcancel\b', r'\breschedule\b',
        r'\btherapist\b', r'\bcoffee beans\b',
    ]
    for filepath in PERSONAL_TASK_FILES:
        try:
            with open(filepath) as f:
                raw = f.read()
            for m in re.finditer(r'^[\s]*-[\s]*\[[\s]*\][\s]*(.+?)$', raw, re.MULTILINE):
                text = m.group(1).strip()
                text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
                text = re.sub(r'`([^`]+)`', r'\1', text)
                text = re.sub(r'🔥|🔁|⏫|⏬|🔽|⚠️|✅|📝|~~[^~]+~~', '', text).strip()
                text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text).strip()
                if not text or len(text) < 5 or len(text) > 100:
                    continue
                if text in tasks:
                    continue
                if not any(re.search(pat, text.lower()) for pat in action_patterns):
                    continue
                if any(skip in text.lower() for skip in (
                    "often gassy", "occasional pain", "discomfort when", "nausea",
                    "stool is", "sometimes it's", "gas and", "symptom", "smoothie",
                    "eating ", "burrito", "sandwich", "stir fry", "roasted veg",
                    "cooked greens", "chicken or fish", "bananas", "white rice",
                    "low or no oil", "old hard video", "new hard video",
                    "relatively continuous", "less symptoms", "back to back",
                    "30 days", "potentially existing")):
                    continue
                tasks[text] = ("admin", filepath)
        except (IOError, UnicodeDecodeError):
            pass

    # 3. Reclassify by keyword
    keyword_cat_map = {
        "cad": "making", "3d print": "making", "print": "making",
        "drone": "flying", "quad": "flying", "fly": "flying", "transmitter": "flying",
        "garden": "garden", "plant": "garden", "weed": "garden", "prune": "garden",
        "trellis": "garden", "gate thing": "garden", "strawberry": "garden",
        "dead plant": "garden", "repot": "garden", "irrigation": "garden",
        "cook": "culinary", "bake": "culinary", "meal": "culinary",
        "clean": "household", "tidy": "household", "declutter": "household",
        "laundry": "household", "dish": "household",
        "doctor": "admin", "appointment": "admin", "insurance": "admin",
        "buy": "admin", "shop": "admin", "order": "admin",
        "insurance": "admin", "pair": "admin", "chino": "admin", "undershirt": "admin",
        "call": "admin", "appointment": "admin", "prescription": "admin",
    }
    reclassified = {}
    for text, (cat, src_path) in tasks.items():
        text_lower = text.lower()
        for kw, newcat in keyword_cat_map.items():
            if kw in text_lower:
                cat = newcat
                break
        reclassified[text] = (cat, src_path)
    tasks = reclassified

    # 4. Inject into database
    count = 0
    # Category → sensible defaults
    cat_defaults = {
        "household": {"phy": "light", "men": "low", "min_time": 15, "max_time": 60,
                      "location": "home", "equipment": "basic", "noise": "quiet"},
        "garden": {"phy": "moderate", "men": "low", "min_time": 30, "max_time": 120,
                   "location": "home", "equipment": "basic", "noise": "quiet",
                   "weather": "required", "hour_window": "daytime", "season": "frost-free"},
        "making": {"phy": "light", "men": "medium", "min_time": 30, "max_time": 180,
                   "location": "home", "equipment": "specialized", "noise": "moderate"},
        "admin": {"phy": "none", "men": "medium", "min_time": 15, "max_time": 60,
                  "location": "anywhere", "equipment": "none", "noise": "silent"},
        "flying": {"phy": "light", "men": "medium", "min_time": 60, "max_time": 180,
                   "location": "home", "equipment": "specialized", "noise": "quiet"},
    }

    for text, (cat, src_path) in tasks.items():
        if text in seen_names:
            continue
        seen_names.add(text)
        d = cat_defaults.get(cat, cat_defaults["admin"])
        conn.execute("""INSERT OR REPLACE INTO activities
            (name, category, subcategory, phy, men, min_time, max_time,
             location, day, hour_window, season, weather, weather_wind, weather_temp,
             equipment, noise, cost, setup_time, teardown_time,
             interrupt, screen, flow, mess, create_consume, social,
             recency_ideal, streak_benefit, base_interest, is_project, is_chore,
             work_type, project_name, next_action, vault_path)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            text, cat, "task",
            d["phy"], d.get("men", "low"), d["min_time"], d["max_time"],
            d["location"], "any", d.get("hour_window", "any"),
            d.get("season", "all-year"), d.get("weather", "no"), "any", "any",
            d["equipment"], d.get("noise", "quiet"),
            "free", "none", "light", "safe", "no", "none", "none", "create", "solo",
            "weekly", 0, 5, 0, 1, cat, None, None, src_path
        ))
        count += 1

    return count

def ensure_fresh(conn: sqlite3.Connection, force=False):
    """Rebuild cache if vault changed since last rebuild."""
    if force:
        rebuild_cache(conn, verbose=True)
        return
    current_hash = vault_mtime_hash()
    row = conn.execute("SELECT value FROM meta WHERE key='vault_hash'").fetchone()
    if not row or row[0] != current_hash:
        rebuild_cache(conn, verbose=True)
    else:
        # Quick sanity: if no activities, rebuild
        count = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
        if count == 0:
            rebuild_cache(conn, verbose=True)

# ── Gate Logic ─────────────────────────────────────────────────────
def get_tod(hour: int) -> str:
    for tod, (s, e) in TIME_OF_DAY.items():
        if s < e and s <= hour < e:
            return tod
        if s > e and (hour >= s or hour < e):
            return tod
    return "any"

def check_hour_window(window, hour):
    if window in ("any", None, ""):
        return True
    window_map = {
        "morning": range(4, 12), "afternoon": range(12, 17),
        "daytime": range(6, 20), "evening": range(17, 21),
        "night": [21, 22, 23, 0, 1, 2, 3],
        "evening-night": range(17, 24), "after-dark": range(20, 24),
        "dawn-dusk": range(6, 20),
    }
    combined = str(window).split("-")
    if len(combined) == 2 and combined[0] in window_map and combined[1] in window_map:
        return hour in set(window_map[combined[0]]) | set(window_map[combined[1]])
    if str(window) in window_map:
        return hour in (set(window_map[str(window)]) if isinstance(window_map[str(window)], list) else set(window_map[str(window)]))
    m = re.match(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", str(window))
    if m:
        sh, _, eh, _ = map(int, m.groups())
        return sh <= hour < eh if sh <= eh else (hour >= sh or hour < eh)
    return True

def check_day(day_spec, day_name):
    if day_spec in ("any", None, ""):
        return True
    if day_spec == "weekend" and day_name in ("Sat", "Sun"):
        return True
    if day_spec == "weekday" and day_name in ("Mon", "Tue", "Wed", "Thu", "Fri"):
        return True
    m = re.match(r"(\w+)-(\w+)", str(day_spec))
    if m:
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        try:
            si, ei, ci = days.index(m.group(1)), days.index(m.group(2)), days.index(day_name)
            return si <= ci <= ei if si <= ei else (ci >= si or ci <= ei)
        except ValueError:
            return True
    return day_name == day_spec

def check_season(season_spec, month):
    if season_spec in ("all-year", None, ""):
        return True
    return month in SEASON_MONTHS.get(season_spec, [])

def apply_gates(row, state):
    if state["time_available"] < (row["min_time"] or 0):
        return False
    loc = row["location"] or "anywhere"
    if state["at_home"] and loc in ("nearby-outdoors", "trail", "specific-location", "range"):
        return False
    if not state["at_home"] and loc == "home":
        return False
    if not check_day(row["day"], state["day_of_week"]):
        return False
    if not check_hour_window(row["hour_window"], state["current_hour"]):
        return False
    if not check_season(row["season"], state["current_month"]):
        return False
    if row["weather"] == "required" and state["weather"] != "good":
        return False
    wind = row["weather_wind"] or "any"
    if wind == "calm" and state["wind"] != "calm":
        return False
    if wind == "moderate-max" and state["wind"] == "strong":
        return False
    if row["noise"] == "loud" and get_tod(state["current_hour"]) in ("night", "late-night"):
        return False
    if row["noise"] == "moderate" and get_tod(state["current_hour"]) == "late-night":
        return False
    return True

# ── Scoring Logic ──────────────────────────────────────────────────
def score_row(row, state, recent_categories=None):
    score = 0.0
    bd = []

    def add(pts, reason):
        nonlocal score
        score += pts
        bd.append(f"{reason}: {pts:+.1f}")

    emap = {"drained": 0, "low": 1, "medium": 2, "high": 3}
    pmap = {"none": 0, "light": 1, "moderate": 2, "intense": 3}
    energy_diff = abs(emap.get(state["physical_energy"], 2) - pmap.get(row["phy"] or "none", 0))
    if energy_diff == 0:
        add(10, "perfect energy match")
    elif energy_diff == 1:
        add(5, "good energy match")
    elif energy_diff >= 3:
        add(-10, "poor energy match")

    mmap = {"fried": 0, "tired": 1, "medium": 2, "fresh": 3}
    mental_diff = abs(mmap.get(state["mental_freshness"], 2) - mmap.get(row["men"] or "medium", 2))
    if mental_diff == 0:
        add(10, "perfect mental match")
    elif mental_diff == 1:
        add(5, "good mental match")
    elif mental_diff >= 3:
        add(-10, "poor mental match")

    max_t = row["max_time"] or 240
    ta = state["time_available"]
    if 0.5 <= ta / max_t <= 1.2:
        add(8, "good time fit")
    elif ta / max_t > 2:
        add(-3, "too much time")
    else:
        add(3, "ok time fit")

    if row["weather"] == "prefer" and state["weather"] != "good":
        add(-5, "bad weather penalty")

    tod = get_tod(state["current_hour"])
    if row["screen"] == "yes" and tod in ("night", "late-night"):
        add(-5, "screen + late night")
    elif row["screen"] == "no" and tod in ("night", "late-night"):
        add(3, "screen-free + night bonus")

    smap = {"none": 0, "light": 1, "moderate": 2, "heavy": 3}
    if smap.get(row["setup_time"], 0) >= 2 and ta < 60:
        add(-5, "heavy setup + short time")

    sp = state.get("social", "either")
    sr = row["social"] or "either"
    if sp == "solo" and sr == "social":
        add(-5, "solo vs social")
    elif sp == "social" and sr == "solo":
        add(-3, "social vs solo")
    elif sp == sr:
        add(3, "social match")

    # Recency
    base_i = row["base_interest"] or 5
    conn = get_db()
    last = conn.execute("SELECT MAX(done_at) FROM recency_log WHERE activity_name=?",
                        (row["name"],)).fetchone()[0]
    if last:
        try:
            days_since = (date.today() - datetime.fromisoformat(last).date()).days
        except (ValueError, TypeError):
            days_since = None
    else:
        days_since = None

    if days_since is not None:
        dr = DECAY_RATES.get(row["recency_ideal"] or "weekly", 0.08)
        decay_score = base_i * math.exp(-dr * min(days_since, 365))
        add(min(decay_score, base_i), f"decay ({base_i}, {days_since}d)")
    else:
        add(base_i, f"decay ({base_i}, never)")
        days_since = 0

    if row["streak_benefit"] and days_since is not None and days_since <= 3:
        add(5, "streak bonus")
    elif row["streak_benefit"] and days_since is not None and days_since <= 7:
        add(2, "streak bonus (week)")

    ideal_days = {"daily": 1, "2-3x-week": 3, "weekly": 7, "biweekly": 14, "monthly": 30, "seasonal": 90}
    ideal = ideal_days.get(row["recency_ideal"] or "weekly", 7)
    if days_since is not None and days_since > 3 * ideal:
        add(-10, f"neglect ({days_since}d > {3*ideal}d)")

    if row["is_project"]:
        add(1, "project base boost")

    if row["is_chore"]:
        priority = 5  # default
        overdue = 7
        edays = days_since if days_since else 0
        urgency = priority * min(3.0, 1.0 + edays / overdue) if edays > 0 else priority * 0.5
        add(urgency, f"chore urgency ({edays}d)")

    if recent_categories:
        cat = row["category"] or ""
        same = sum(1 for c in recent_categories if c == cat)
        if same >= 3:
            add(-5, f"diversity penalty ({cat})")
        elif same == 0:
            add(5, "diversity bonus")

    return {"score": max(score, 0), "breakdown": bd, "row": row}

# ── Commands ───────────────────────────────────────────────────────
def cmd_suggest(args):
    conn = get_db()
    init_db(conn)
    ensure_fresh(conn, force=args.rebuild)

    now = datetime.now()
    day = args.day or now.strftime("%a")
    hour = args.hour if args.hour is not None else now.hour
    energy, mental, is_work, is_dinner, is_late = resolve_rhythm(day, hour, args.energy, args.mental)

    state = {
        "time_available": args.time,
        "physical_energy": energy,
        "mental_freshness": mental,
        "at_home": args.at_home,
        "weather": args.weather,
        "temperature": args.temp,
        "wind": args.wind,
        "social": args.social,
        "day_of_week": day,
        "current_hour": hour,
        "current_month": now.month,
        "is_work_hours": is_work,
        "is_dinner": is_dinner,
        "is_late_night": is_late,
    }

    rows = conn.execute("SELECT * FROM activities").fetchall()
    if not rows:
        print("No activities in cache. Run with --rebuild first.")
        return

    passed, eliminated = [], 0
    for r in rows:
        if apply_gates(r, state):
            passed.append(r)
        else:
            eliminated += 1

    if not passed:
        results = [score_row(r, state) for r in rows if r["name"] in SAFETY_VALVE_NAMES]
        results.sort(key=lambda x: x["score"], reverse=True)
        note = "(safety valve)"
    else:
        results = [score_row(r, state) for r in passed]
        results.sort(key=lambda x: x["score"], reverse=True)
        note = None

    top = results[:args.count]

    if args.json:
        out = {
            "state": state,
            "total": len(rows), "eliminated": eliminated, "passed": len(passed),
            "safety_valve": note is not None,
            "suggestions": [
                {"name": r["row"]["name"],
                 "category": r["row"]["category"],
                 "score": round(r["score"], 1),
                 "min_time": r["row"]["min_time"],
                 "is_project": bool(r["row"]["is_project"]),
                 "is_chore": bool(r["row"]["is_chore"]),
                 "breakdown": r["breakdown"] if args.explain else []}
                for r in top
            ],
        }
        print(json.dumps(out, indent=2))
    else:
        print(f"State: {state['day_of_week']} {state['current_hour']:02d}:00, "
              f"{state['time_available']}min, energy={state['physical_energy']}, "
              f"mental={state['mental_freshness']}, "
              f"{'home' if state['at_home'] else 'away'}, weather={state['weather']}")
        print(f"Total: {len(rows)} | Eliminated: {eliminated} | Passed: {len(passed)}")
        if note:
            print(f"\n  {note}\n")

        for i, r in enumerate(top, 1):
            a = r["row"]
            cat = a["category"] or "???"
            proj_tag = ""
            if a["is_project"]:
                prefix = WORK_TYPE_LABELS.get(a["work_type"] or "", "")
                pname = a["project_name"] or ""
                proj_tag = f" [{prefix} {pname}]" if prefix else f" [{pname}]"
            chore_tag = " [CHORE]" if a["is_chore"] else ""
            name = a["name"] or "?"
            if len(name) > 55:
                name = name[:52] + "..."
            print(f"  {i:2d}. {name:<55s} {r['score']:5.1f}  "
                  f"({cat}, {a['min_time'] or 0}m, {a['location'] or '?'}){proj_tag}{chore_tag}")
            if args.explain and r.get("breakdown"):
                for b in r["breakdown"]:
                    print(f"      {b}")
    conn.close()

def cmd_done(args):
    conn = get_db()
    init_db(conn)
    activity_name = args.done
    # Fuzzy match
    row = conn.execute("SELECT name FROM activities WHERE name LIKE ?",
                       (f"%{activity_name}%",)).fetchone()
    if not row:
        print(f"Activity not found: {activity_name}")
        conn.close()
        return
    name = row[0]
    conn.execute("INSERT INTO recency_log (activity_name, duration_minutes) VALUES (?, ?)",
                 (name, args.duration))
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM recency_log WHERE activity_name=?", (name,)).fetchone()[0]
    print(f"Logged: {name} ({args.duration or '?'} min) — done {count} times total")
    conn.close()

def cmd_stats(args):
    conn = get_db()
    init_db(conn)
    days = args.days or 30
    rows = conn.execute("""
        SELECT activity_name, COUNT(*) as cnt, MAX(done_at) as last_done
        FROM recency_log
        WHERE done_at >= date('now', ?)
        GROUP BY activity_name
        ORDER BY cnt DESC
    """, (f"-{days} days",)).fetchall()

    print(f"Activity history — last {days} days\n")
    print(f"  {'Activity':<40s} {'Count':>5s}  {'Last Done':>16s}")
    print(f"  {'─'*40} {'─'*5}  {'─'*16}")
    for r in rows:
        print(f"  {r[0]:<40s} {r[1]:>5d}  {r[2] or '':>16s}")
    if not rows:
        print("  (no activity logged in this period)")
    print(f"\n  Total entries: {sum(r[1] for r in rows)}")

    # Neglected activities
    neglected = conn.execute("""
        SELECT a.name, a.category, a.recency_ideal, a.base_interest,
               COALESCE((SELECT julianday('now') - julianday(MAX(done_at))
                         FROM recency_log WHERE activity_name = a.name), 999) as days_since
        FROM activities a
        WHERE a.is_chore = 0
        ORDER BY days_since DESC
        LIMIT 10
    """).fetchall()

    print(f"\n  Most neglected activities:")
    for r in neglected:
        ideal = r[2] or "weekly"
        days = int(r[4]) if r[4] else 999
        flag = " ⚠️" if days > 30 else ""
        print(f"    {r[0]:<45s} {days:>4d}d since  (ideal: {ideal}){flag}")
    conn.close()


def cmd_browse(args):
    """Interactive decision-tree activity browser."""
    conn = get_db()
    init_db(conn)
    ensure_fresh(conn)

    now = datetime.now()
    day = args.day if hasattr(args, "day") and args.day else now.strftime("%a")
    hour = args.hour if hasattr(args, "hour") and args.hour is not None else now.hour
    energy, mental, is_work, is_dinner, is_late = resolve_rhythm(day, hour, args.energy, args.mental)

    constraints = {
        "time_available": args.time,
        "physical_energy": energy,
        "mental_freshness": mental,
        "at_home": args.at_home,
        "weather": args.weather,
        "wind": args.wind,
        "temperature": None,
        "social": args.social,
        "day_of_week": day,
        "current_hour": hour,
        "current_month": now.month,
        "is_work_hours": is_work,
        "is_dinner": is_dinner,
        "is_late_night": is_late,
    }

    # Quick shortcuts
    if args.surprise:
        _quick_surprise(conn, constraints)
        conn.close()
        return
    if args.intent:
        _quick_intent(conn, constraints, args.intent)
        conn.close()
        return

    session = BrowseSession(constraints=constraints)
    _print_header(constraints)

    while True:
        ld = get_level_data(conn, session.constraints, session.scope)
        session.set_level_data(ld)

        if ld["level_name"] == "safety_valve":
            _render_safety_valve(ld)
        elif ld["level_name"] == "intent":
            _render_intents(ld)
        elif ld["level_name"] == "category":
            _render_categories(ld)
        elif ld["level_name"] == "activity":
            if len(ld["groups"]) == 1:
                _render_activity_detail(ld["groups"][0])
            else:
                _render_activity_list(ld)

        print()
        choice = input("  > ").strip().lower()
        if not choice:
            continue

        if choice == 's':
            act = surprise(conn, session.constraints, session.scope, session.seen)
            if act:
                session.seen.add(act["name"])
                # Show it as single activity view
                g = {"key": act["name"], "label": act["name"], "count": 1,
                     "representative": act, "min_time": act.get("min_time"),
                     "max_time": act.get("max_time"), "category": act.get("category"),
                     "recency": act.get("_recency",""), "tags": act.get("_tags",[])}
                _render_activity_detail(g)
                print()
                choice2 = input("  > ").strip().lower()
                if choice2 == '1' or choice2 == '2':
                    _log_activity(conn, act["name"])
                    conn.close()
                    return
                elif choice2 == 'q':
                    conn.close()
                    return
                continue
            else:
                print("  (nothing new to surprise with — try a different scope)")
                continue

        if choice == 'q':
            conn.close()
            return

        action = session.advance(choice)

        if action == 'exit':
            conn.close()
            return
        elif action == 'log_and_exit':
            act = session.get_selected_activity()
            name = act["name"] if act else (ld["groups"][0]["key"] if ld["groups"] else "unknown")
            _log_activity(conn, name)
            conn.close()
            return
        elif action == 'back':
            continue
        elif action == 'surprise':
            act = surprise(conn, session.constraints, session.scope, session.seen)
            if act:
                session.seen.add(act["name"])
                g = {"key": act["name"], "label": act["name"], "count": 1,
                     "representative": act, "min_time": act.get("min_time"),
                     "max_time": act.get("max_time"), "category": act.get("category"),
                     "recency": act.get("_recency",""), "tags": act.get("_tags",[])}
                _render_activity_detail(g)
                print()
                choice2 = input("  > ").strip().lower()
                if choice2 == '1' or choice2 == '2':
                    _log_activity(conn, act["name"])
                    conn.close()
                    return
                elif choice2 == 'q':
                    conn.close()
                    return
        elif action == 'detail_view':
            # Show single activity detail from the selection
            ld = session._last_level_data
            if ld and ld.get("groups"):
                _render_activity_detail(ld["groups"][0])
                print()
                choice3 = input("  > ").strip().lower()
                if choice3 == '1' or choice3 == '2':
                    name = ld["groups"][0]["key"]
                    _log_activity(conn, name)
                    conn.close()
                    return
                elif choice3 == '3':
                    act = session.get_selected_activity()
                    if not act and ld["groups"]:
                        act = ld["groups"][0].get("representative", {})
                    if act:
                        detail = get_activity_detail(conn, act.get("name", ld["groups"][0]["key"]))
                        if detail:
                            print(f"\n  📋 {detail['name']}")
                            print(f"     Category: {detail.get('category','?')}")
                            print(f"     Time: {detail.get('min_time',0)}–{detail.get('max_time',240)} min")
                            print(f"     Location: {detail.get('location','?')}")
                            print(f"     Equipment: {detail.get('equipment','?')}")
                            if detail.get("vault_path"):
                                print(f"     Vault: {detail['vault_path']}")
                    print()
                elif choice3 == 's':
                    act = surprise(conn, session.constraints, session.scope, session.seen)
                    if act:
                        session.seen.add(act["name"])
                        g = {"key":act["name"],"label":act["name"],"count":1,
                             "representative":act,"min_time":act.get("min_time"),
                             "max_time":act.get("max_time"),"category":act.get("category"),
                             "recency":act.get("_recency",""),"tags":act.get("_tags",[])}
                        _render_activity_detail(g)
                        print()
                        choice4 = input("  > ").strip().lower()
                        if choice4 == '1' or choice4 == '2':
                            _log_activity(conn, act["name"])
                            conn.close()
                            return
                        elif choice4 == 'q':
                            conn.close()
                            return
                elif choice3 == 'b':
                    session.back()
                elif choice3 == 'q':
                    conn.close()
                    return
            continue
        elif action == 'continue':
            continue
        elif action == 'invalid':
            print("  ? — try a number, [s]urprise, [b]ack, or [q]uit")
        elif action == 'tell_more':
            act = session.get_selected_activity()
            if act:
                detail = get_activity_detail(conn, act["name"])
                if detail:
                    print(f"\n  📋 {detail['name']}")
                    print(f"     Category: {detail.get('category','?')}")
                    print(f"     Time: {detail.get('min_time',0)}–{detail.get('max_time',240)} min")
                    print(f"     Location: {detail.get('location','?')}")
                    print(f"     Equipment: {detail.get('equipment','?')}")
                    print(f"     Tags: {', '.join(detail.get('_tags',[]))}")
                    print(f"     Recency: {detail.get('_recency','?')}")
                    if detail.get("vault_path"):
                        print(f"     Vault: {detail['vault_path']}")
                    if detail.get("_times_done",0) > 0:
                        print(f"     Done: {detail['_times_done']} times")
            print()
            continue

    conn.close()


def _quick_surprise(conn, constraints):
    act = surprise(conn, constraints, "all", set())
    if act:
        print(f"\n  🎲 {act['name']}")
        print(f"     {act.get('_recency','')}  ·  {act.get('min_time',0)}–{act.get('max_time',240)} min")
        print(f"     Category: {act.get('category','?')}")
        print()
        c = input("  Log it? [y/N] ").strip().lower()
        if c == 'y':
            _log_activity(conn, act["name"])
    else:
        print("  Nothing available right now.")


def _quick_intent(conn, constraints, intent):
    session = BrowseSession(constraints=constraints, path=[intent], scope=f"intent:{intent}")
    ld = get_level_data(conn, constraints, f"intent:{intent}")
    if ld["groups"]:
        _render_categories(ld) if ld["level_name"]=="category" else _render_activity_list(ld)
        _print_prompt()
        choice = input("  > ").strip().lower()
        if choice == 's':
            act = surprise(conn, constraints, f"intent:{intent}")
            if act:
                print(f"\n  🎲 {act['name']}  ·  {act.get('min_time',0)}–{act.get('max_time',240)} min\n")
                c = input("  Log it? [y/N] ").strip().lower()
                if c == 'y': _log_activity(conn, act["name"])
        elif choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(ld["groups"]):
                g = ld["groups"][idx]
                print(f"\n  ✅ Picked: {g['label']}")
                c = input("  Log it? [y/N] ").strip().lower()
                if c == 'y': _log_activity(conn, g["key"])
    conn.close()


def _log_activity(conn, name):
    conn.execute("INSERT INTO recency_log (activity_name) VALUES (?)", (name,))
    conn.commit()
    print(f"  ✅ Logged: {name}")


# ── Rendering ──────────────────────────────────────────────────────
def _print_header(c):
    loc = "at home" if c["at_home"] else "away"
    rhythm_note = ""
    if c.get("is_work_hours"): rhythm_note = " [work hours]"
    elif c.get("is_dinner"): rhythm_note = " [dinner time]"
    elif c.get("is_late_night"): rhythm_note = " [late night]"
    print(f"\n  🌤️  {c['day_of_week']} {c['current_hour']:02d}:00  ·  "
          f"{c['time_available']}min  ·  {loc}  ·  {c['weather']}{rhythm_note}")
    print(f"     Energy: {c.get('physical_energy','?')}  ·  Mental: {c.get('mental_freshness','?')}")



def _print_prompt():
    print(f"\n    [s] Surprise    [b] back    [q] quit")


def _render_safety_valve(ld):
    print(f"\n  ⚠️  {ld['title']}\n")
    for i, g in enumerate(ld["groups"], 1):
        print(f"    [{i}] {g['label']:<30s} {g['min_time'] or '?'}–{g['max_time'] or '?'} min")
    _print_prompt()


def _render_intents(ld):
    print(f"\n  {ld['title']}\n")
    for i, g in enumerate(ld["groups"], 1):
        print(f"    [{i}] {g['label']:<30s} ({g['count']})")
    print(f"\n    [s] 🎲 Surprise me    [q] quit")


def _render_categories(ld):
    print(f"\n  {ld['title']}\n")
    for i, g in enumerate(ld["groups"], 1):
        print(f"    [{i}] {g['label']:<30s} ({g['count']})")
    _print_prompt()


def _render_activity_list(ld):
    print(f"\n  {ld['title']}\n")
    max_show = 8
    groups = ld["groups"]
    for i, g in enumerate(groups[:max_show], 1):
        rec = g.get("recency","")
        t = f"{g['min_time'] or '?'}–{g['max_time'] or '?'}m"
        print(f"    [{i}] {g['label']:<35s} {t:<10s} {rec}")
    if len(groups) > max_show:
        print(f"    ... ({len(groups)-max_show} more — [s]urprise or type a number)")
    _print_prompt()


def _render_activity_detail(g):
    a = g.get("representative", {})
    name = g["label"]
    t = f"{g['min_time'] or '?'}–{g['max_time'] or '?'} min"
    tags = "  ·  ".join(g.get("tags",[]))
    rec = g.get("recency","")
    print(f"\n  {name}")
    print(f"    {t}  ·  {tags}")
    if rec:
        print(f"    {rec}")
    print(f"\n    [1] Let's do this    [2] I did this (log it)")
    print(f"    [3] Tell me more")
    print(f"    [s] Surprise me something else")
    print(f"    [b] Back    [q] quit")

# ── Main ───────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="Activity Suggester (SQLite-backed)")
    sp = p.add_subparsers(dest="command")

    # suggest (default)
    sug = sp.add_parser("suggest", help="Get activity suggestions (default)")
    sug.add_argument("--time", type=int, default=120)
    sug.add_argument("--energy", default=None, choices=["drained", "low", "medium", "high"])
    sug.add_argument("--mental", default=None, choices=["fried", "tired", "medium", "fresh"])
    sug.add_argument("--home", dest="at_home", action="store_true", default=True)
    sug.add_argument("--away", dest="at_home", action="store_false")
    sug.add_argument("--weather", default="good", choices=["good", "rain", "wind", "cold", "hot"])
    sug.add_argument("--temp", type=int, default=None)
    sug.add_argument("--wind", default="calm", choices=["calm", "light", "moderate", "strong"])
    sug.add_argument("--social", default="either", choices=["solo", "either", "social"])
    sug.add_argument("--day", default=None)
    sug.add_argument("--hour", type=int, default=None)
    sug.add_argument("--count", type=int, default=8)
    sug.add_argument("--explain", action="store_true")
    sug.add_argument("--json", action="store_true")
    sug.add_argument("--rebuild", action="store_true", help="Force cache rebuild")

    # done
    dn = sp.add_parser("done", help="Log a completed activity")
    dn.add_argument("--done", required=True, help="Activity name (fuzzy match)")
    dn.add_argument("--duration", type=int, default=None, help="Minutes spent")

    # stats
    st = sp.add_parser("stats", help="Show activity history and neglected activities")
    st.add_argument("--days", type=int, default=30, help="Days of history")

    # rebuild
    sp.add_parser("rebuild", help="Force cache rebuild from vault")

    # browse
    brw = sp.add_parser("browse", help="Interactive decision-tree activity browser")
    brw.add_argument("--time", type=int, default=120)
    brw.add_argument("--energy", default=None, choices=["drained","low","medium","high"])
    brw.add_argument("--mental", default=None, choices=["fried","tired","medium","fresh"])
    brw.add_argument("--home", dest="at_home", action="store_true", default=True)
    brw.add_argument("--away", dest="at_home", action="store_false")
    brw.add_argument("--weather", default="good", choices=["good","rain","wind","cold","hot"])
    brw.add_argument("--wind", default="calm", choices=["calm","light","moderate","strong"])
    brw.add_argument("--social", default="either", choices=["solo","either","social"])
    brw.add_argument("--surprise", action="store_true", help="Skip tree, get one random pick")
    brw.add_argument("--intent", default=None, help="Skip to intent (make/fix/move/learn/relax)")
    brw.add_argument("--day", default=None, help="Day override (Mon, Tue, ...)")
    brw.add_argument("--hour", type=int, default=None, help="Hour override (0-23)")
    brw.add_argument("--strict", action="store_true", help="Hide time-incompatible activities")

    # Also support top-level flags (backward compat)
    args, unknown = p.parse_known_args()
    if not args.command and unknown:
        # Top-level flags without subcommand
        args2 = argparse.Namespace(
            command="suggest", time=120, energy=None, mental=None,
            at_home=True, weather="good", temp=None, wind="calm", social="either",
            day=None, hour=None, count=8, explain=False, json=False, rebuild=False
        )
        sug.parse_args(unknown, namespace=args2)
        args = args2
    elif not args.command:
        args.command = "suggest"

    if args.command == "suggest":
        cmd_suggest(args)
    elif args.command == "done":
        cmd_done(args)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "browse":
        cmd_browse(args)
    elif args.command == "rebuild":
        conn = get_db()
        init_db(conn)
        rebuild_cache(conn, verbose=True)
        conn.close()

if __name__ == "__main__":
    main()
