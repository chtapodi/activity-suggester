#!/usr/bin/env python3
"""Activity Suggester — Daily Suggestion Engine with Telegram Delivery & Feedback.

Usage (dry-run / test):
  python3 activity-daily-suggest.py --dry-run

Usage (normal):
  python3 activity-daily-suggest.py

Creates/uses a suggestions table to track accept/dismiss feedback.
Delivers suggestions via Telegram Bot API (stdlib only).
Designed to be called from a cron job.
"""

import json
import os
import random
import sqlite3
import sys
import time
import math
from datetime import datetime, date, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# ── Paths ───────────────────────────────────────────────────────────
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS_DIR)
from browse_core import apply_gates, resolve_rhythm, DECAY_RATES
from ha_context import get_ha_context

CACHE_DB = os.path.expanduser("~/.hermes/data/activity-cache.db")
SECRETS_DIR = os.path.join(os.path.expanduser("~"), ".hermes", "secrets")
TOKEN_FILE = os.path.join(SECRETS_DIR, "telegram-bot-token")
CHAT_ID = 385284769  # Xavier's chat ID

SAFETY_VALVE_NAMES = {"Meditation", "Napping", "Music Listening", "Reading (Light)",
                      "Yoga and Stretching", "Browsing Online"}

# ── Delivery Windows ────────────────────────────────────────────────
DELIVERY_WINDOWS = {
    "after_work": {
        "days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
        "hour": 17,
        "minute": 15,
        "time_available": 90,
        "count": 1,
        "label": "After work wind-down",
        "theme": "restorative-light",
    },
    "weekend_morning": {
        "days": ["Sat", "Sun"],
        "hour": 9,
        "minute": 0,
        "time_available": 240,
        "count": 3,
        "label": "Weekend morning",
        "theme": "full-day",
    },
}


# ══════════════════════════════════════════════════════════════════════
# Telegram API
# ══════════════════════════════════════════════════════════════════════

def _read_token():
    """Read Telegram bot token from secrets file."""
    try:
        with open(TOKEN_FILE) as f:
            token = f.read().strip()
        if not token:
            print("ERROR: Empty bot token", file=sys.stderr)
            return None
        return token
    except FileNotFoundError:
        print(f"ERROR: Token file not found: {TOKEN_FILE}", file=sys.stderr)
        return None
    except OSError as e:
        print(f"ERROR: Cannot read token file: {e}", file=sys.stderr)
        return None


def _api_call(token, method, params=None, retries=3):
    """Make a Telegram Bot API call with rate limit handling."""
    if params is None:
        params = {}
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(params).encode("utf-8")
    headers = {"Content-Type": "application/json"}

    for attempt in range(retries):
        try:
            req = Request(url, data=data, headers=headers, method="POST")
            with urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            if not result.get("ok"):
                print(f"WARNING: API returned not ok for {method}: {result}", file=sys.stderr)
                return None
            return result.get("result")
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            try:
                err_data = json.loads(body)
            except json.JSONDecodeError:
                err_data = {}
            if e.code == 429:
                retry_after = err_data.get("parameters", {}).get("retry_after", 5)
                print(f"Rate limited. Sleeping {retry_after}s...", file=sys.stderr)
                time.sleep(retry_after + 1)
                continue
            print(f"ERROR: HTTP {e.code} on {method}: {body}", file=sys.stderr)
            if attempt < retries - 1:
                time.sleep(2)
                continue
            return None
        except (URLError, OSError, json.JSONDecodeError) as e:
            print(f"ERROR: {e} on {method}", file=sys.stderr)
            if attempt < retries - 1:
                time.sleep(3)
                continue
            return None
    return None


def send_message(text, parse_mode="HTML", disable_web_page_preview=True):
    """Send a Telegram message. Returns message_id on success, None on failure."""
    token = _read_token()
    if not token:
        return None
    params = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": disable_web_page_preview,
    }
    if parse_mode:
        params["parse_mode"] = parse_mode
    result = _api_call(token, "sendMessage", params)
    if result:
        return result.get("message_id")
    return None


def delete_message(message_id):
    """Delete a Telegram message by message_id."""
    token = _read_token()
    if not token or not message_id:
        return False
    params = {"chat_id": CHAT_ID, "message_id": message_id}
    result = _api_call(token, "deleteMessage", params)
    return result is not None


def edit_message(message_id, text, parse_mode="HTML"):
    """Edit a Telegram message."""
    token = _read_token()
    if not token or not message_id:
        return None
    params = {
        "chat_id": CHAT_ID,
        "message_id": message_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        params["parse_mode"] = parse_mode
    return _api_call(token, "editMessageText", params)


def set_reactions(message_id, reactions):
    """Set reactions on a message. reactions is a list of emoji strings (max 1 for free bots)."""
    token = _read_token()
    if not token or not message_id:
        return False
    params = {
        "chat_id": CHAT_ID,
        "message_id": message_id,
        "reaction": [{"type": "emoji", "emoji": r} for r in reactions[:1]],
    }
    result = _api_call(token, "setMessageReaction", params)
    return result is not None


# ══════════════════════════════════════════════════════════════════════
# Database helpers
# ══════════════════════════════════════════════════════════════════════

def get_db():
    """Connect to activity cache DB."""
    conn = sqlite3.connect(CACHE_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_suggestions_table(conn):
    """Create suggestions tracking table for feedback."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS suggestions_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            suggested_at TEXT NOT NULL DEFAULT (datetime('now')),
            activity_name TEXT NOT NULL,
            window_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            responded_at TEXT,
            duration_minutes INTEGER,
            note TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_suggestions_date ON suggestions_log(suggested_at);
        CREATE INDEX IF NOT EXISTS idx_suggestions_status ON suggestions_log(status);
    """)
    conn.commit()


log_suggestions_table_init = False


def ensure_suggestions_table():
    global log_suggestions_table_init
    conn = get_db()
    init_suggestions_table(conn)
    conn.close()
    if not log_suggestions_table_init:
        print("  ✓ Suggestions feedback table ready", file=sys.stderr)
        log_suggestions_table_init = True


def log_suggestion(conn, activity_name, window_name):
    """Record a suggestion being delivered."""
    conn.execute(
        "INSERT INTO suggestions_log (activity_name, window_name, status) VALUES (?, ?, 'pending')",
        (activity_name, window_name),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def update_suggestion_status(suggestion_id, status, duration=None, note=None):
    """Update feedback on a suggestion: 'accepted', 'dismissed', 'skipped'."""
    conn = get_db()
    conn.execute(
        "UPDATE suggestions_log SET status=?, responded_at=datetime('now'), duration_minutes=?, note=? WHERE id=?",
        (status, duration, note, suggestion_id),
    )
    conn.commit()
    conn.close()


# ══════════════════════════════════════════════════════════════════════
# Suggestion Engine
# ══════════════════════════════════════════════════════════════════════

def get_current_state(time_available):
    """Build constraints dict from current time + HA context."""
    now = datetime.now()
    day = now.strftime("%a")
    hour = now.hour
    energy, mental, is_work, is_dinner, is_late = resolve_rhythm(day, hour)
    ha = get_ha_context()

    return {
        "time_available": time_available,
        "physical_energy": energy,
        "mental_freshness": mental,
        "at_home": ha.get("at_home", True),
        "weather": "good" if ha.get("is_daylight") is not False else "good",
        "temperature": ha.get("temperature"),
        "wind": "calm",
        "social": "solo",
        "day_of_week": day,
        "current_hour": hour,
        "current_month": now.month,
        "is_work_hours": is_work,
        "is_dinner": is_dinner,
        "is_late_night": is_late,
        "is_daylight": ha.get("is_daylight"),
    }


def score_for_suggestion(row, state, conn):
    """Simple score: recency decay + energy/mental/time fit + diversity."""
    score = 0.0
    reasons = []

    # ── Energy match ──
    emap = {"drained": 0, "low": 1, "medium": 2, "high": 3}
    pmap = {"none": 0, "light": 1, "moderate": 2, "intense": 3}
    energy_diff = abs(emap.get(state["physical_energy"], 2) - pmap.get(row["phy"] or "none", 0))
    if energy_diff == 0:
        score += 20  # weighted heavily for suggestions
        reasons.append("energy_perfect")
    elif energy_diff == 1:
        score += 10
        reasons.append("energy_good")
    elif energy_diff >= 3:
        score -= 10
        reasons.append("energy_poor")

    # ── Mental match ──
    mmap = {"fried": 0, "tired": 1, "medium": 2, "fresh": 3}
    mental_diff = abs(mmap.get(state["mental_freshness"], 2) - mmap.get(row["men"] or "medium", 2))
    if mental_diff == 0:
        score += 15
        reasons.append("mental_perfect")
    elif mental_diff == 1:
        score += 8
        reasons.append("mental_good")
    elif mental_diff >= 3:
        score -= 8
        reasons.append("mental_poor")

    # ── Time fit ──
    max_t = row["max_time"] or 240
    ta = state["time_available"]
    ratio = ta / max_t if max_t > 0 else 1
    if 0.4 <= ratio <= 1.5:
        score += 12
        reasons.append("time_fit")
    elif ratio > 2:
        score -= 5
        reasons.append("too_much_time")
    else:
        score += 5
        reasons.append("time_ok")

    # ── Recency decay + base interest ──
    base_i = row["base_interest"] or 5
    last = conn.execute(
        "SELECT MAX(done_at) FROM recency_log WHERE activity_name=?",
        (row["name"],),
    ).fetchone()[0]
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
        score += decay_score
        reasons.append(f"recency_{days_since}d")
    else:
        score += base_i + 5  # never done → bonus
        reasons.append("never_done")

    # ── Screen activities late night penalty ──
    tod_hour = state["current_hour"]
    if row["screen"] == "yes" and (tod_hour >= 22 or tod_hour < 5):
        score -= 8
        reasons.append("late_screen_penalty")
    elif row["screen"] == "no" and (tod_hour >= 22 or tod_hour < 5):
        score += 5
        reasons.append("screen_free_night")

    # ── Projects get slight boost ──
    if row["is_project"]:
        score += 3
        reasons.append("project")

    # ── Chores get urgency boost ──
    if row["is_chore"]:
        overdue = days_since if (days_since is not None and days_since > 0) else 0
        urgency = 5 * min(3.0, 1.0 + overdue / 7)
        score += urgency
        reasons.append(f"chore_urg{overdue}d")

    return {"score": max(score, 0), "reasons": reasons, "days_since": days_since}


def build_suggestions(window_name, count=3):
    """Generate N suggestions using gate + score, ensuring category diversity."""
    conn = get_db()
    window = DELIVERY_WINDOWS.get(window_name)
    if not window:
        print(f"ERROR: Unknown delivery window '{window_name}'", file=sys.stderr)
        conn.close()
        return []

    time_available = window["time_available"]
    state = get_current_state(time_available)

    # ── Fetch all activities ──
    rows = conn.execute("SELECT * FROM activities").fetchall()
    if not rows:
        print("ERROR: No activities in cache. Run activity-suggest --rebuild first.", file=sys.stderr)
        conn.close()
        return []

    # ── Gate + score ──
    scored = []
    for r in rows:
        row = dict(r)
        if not apply_gates(row, state):
            continue
        s = score_for_suggestion(row, state, conn)
        scored.append({**s, "row": row})

    # Safety valve: if nothing passed gates, use safety valve activities
    if not scored:
        fallback = [
            dict(r) for r in rows
            if r["name"] in SAFETY_VALVE_NAMES
        ]
        for row in fallback:
            s = score_for_suggestion(row, state, conn)
            scored.append({**s, "row": row})

    # ── Sort by score ──
    scored.sort(key=lambda x: x["score"], reverse=True)

    # ── Diversity pick: ensure category variety ──
    selected = []
    seen_categories = set()
    seen_names = set()

    for item in scored:
        if len(selected) >= count:
            break
        name = item["row"]["name"]
        cat = item["row"]["category"] or "unknown"
        # Skip if same category already picked (unless we'd run out of options)
        if cat in seen_categories and len(seen_categories) < len(set(s["row"]["category"] for s in scored)):
            # Still allow if the same category has very high score (>2x the lowest selected)
            lowest_selected = min((s["score"] for s in selected), default=0)
            if item["score"] < lowest_selected * 1.5:
                continue
        if name in seen_names:
            continue
        seen_categories.add(cat)
        seen_names.add(name)
        selected.append(item)

    # If still not enough, fill with remaining top scorers
    if len(selected) < count:
        for item in scored:
            if len(selected) >= count:
                break
            if item["row"]["name"] not in seen_names:
                selected.append(item)
                seen_names.add(item["row"]["name"])

    # ── Log suggestions ──
    for item in selected:
        item["suggestion_id"] = log_suggestion(conn, item["row"]["name"], window_name)

    conn.close()
    return {"suggestions": selected, "state": state, "window": window_name}


# ══════════════════════════════════════════════════════════════════════
# Message Formatting
# ══════════════════════════════════════════════════════════════════════

def format_suggestion_message(result):
    """Format suggestions as a Telegram-friendly HTML message."""
    suggestions = result["suggestions"]
    state = result["state"]
    window = result["window"]
    window_config = DELIVERY_WINDOWS[window]

    now = datetime.now()
    emoji_time = "🌤️" if 6 <= state["current_hour"] < 20 else "🌙"

    # Header
    day_name = state["day_of_week"]
    hour = state["current_hour"]
    energy = state["physical_energy"]
    mental = state["mental_freshness"]
    
    lines = [
        f"{emoji_time} <b>Activity suggestions for {day_name}</b> ({window_config['label']})",
        f"   {hour}:00 · {energy} energy · {mental} mental · "
        f"{window_config['time_available']}min available",
        "",
    ]

    # Weather context if available
    ha = get_ha_context()
    temp = ha.get("temperature")
    if temp is not None:
        lines.append(f"   🌡️ {temp}°C indoors")
    if ha.get("is_daylight") is False:
        lines.append("   🌙 It's dark out — outdoor activities blocked")
    elif ha.get("is_daylight") is True:
        lines.append("   ☀️ Daylight hours — go outside!")
    lines.append("")

    # Suggestions
    for i, sug in enumerate(suggestions, 1):
        row = sug["row"]
        name = row["name"]
        min_t = row["min_time"] or 0
        max_t = row["max_time"] or 240
        cat = row["category"] or "?"
        ds = sug["days_since"]
        score = round(sug["score"], 0)

        # Tag
        tags = []
        if row["is_chore"]:
            tags.append("🧹 chore")
        if row["is_project"]:
            tags.append("🛠️ project")
        if row["screen"] == "yes":
            tags.append("💻 screen")
        tag_str = f" ({', '.join(tags)})" if tags else ""

        # Recency note
        if ds is None:
            recency_note = "🆕 You've never done this!"
        elif ds == 0:
            recency_note = "✅ Done today!"
        elif ds == 1:
            recency_note = "✅ Done yesterday"
        elif ds <= 3:
            recency_note = f"✅ Done {ds} days ago"
        elif ds <= 7:
            recency_note = f"⏰ {ds} days ago — due for a repeat"
        elif ds <= 14:
            recency_note = f"👀 {ds} days since last time"
        elif ds <= 30:
            recency_note = f"🌵 {ds} days neglected!"
        else:
            recency_note = f"🔥 {ds} days — very neglected!"

        medal = {1: "🥇", 2: "🥈", 3: "🥉", 4: "4️⃣", 5: "5️⃣"}.get(i, f"{i}.")
        
        lines.append(f"{medal} <b>{name}</b> ({min_t}–{max_t}min · {cat}){tag_str}")
        lines.append(f"   {recency_note}")
        
        # Next action for projects
        if row["next_action"]:
            na = row["next_action"]
            if len(na) > 80:
                na = na[:77] + "..."
            lines.append(f"   → Next: {na}")
        lines.append("")

    # Footer with feedback instructions
    lines.append("━" * 20)
    lines.append("")
    lines.append("React with 👍 to accept a suggestion, 👎 to dismiss.")
    lines.append("Or reply with <code>/done &lt;name&gt;</code> when you complete one.")
    lines.append(f"Full browser: <code>activity-suggest browse --time {window_config['time_available']}</code>")
    lines.append("")

    return "\n".join(lines)


def format_compact_suggestions(result):
    """Format top suggestion in compact format for after_work window."""
    suggestions = result["suggestions"]
    state = result["state"]
    window = result["window"]
    window_config = DELIVERY_WINDOWS[window]

    now = datetime.now()
    emoji_time = "🌤️" if 6 <= state["current_hour"] < 20 else "🌙"
    day_name = state["day_of_week"]

    lines = [
        f"{emoji_time} <b>One thing for {day_name} evening</b>",
        f"   {state['current_hour']}:00 · {state['physical_energy']} energy "
        f"· {window_config['time_available']}min available",
        "",
    ]

    if suggestions:
        sug = suggestions[0]
        row = sug["row"]
        name = row["name"]
        min_t = row["min_time"] or 0
        max_t = row["max_time"] or 240
        ds = sug["days_since"]

        if ds is None:
            recency_note = "🆕 You've never tried this!"
        elif ds == 0:
            recency_note = "✅ Done today"
        elif ds <= 3:
            recency_note = f"✅ {ds} days ago"
        elif ds <= 7:
            recency_note = f"⏰ {ds} days — due"
        else:
            recency_note = f"🔥 {ds} days neglected"

        lines.append(f"🎯 <b>{name}</b> ({min_t}–{max_t}min)")
        lines.append(f"   {recency_note}")
        if row["next_action"]:
            na = row["next_action"]
            if len(na) > 100:
                na = na[:97] + "..."
            lines.append(f"   → {na}")
        lines.append("")

    lines.append("━" * 12)
    lines.append("Reply <code>/done</code> when you do it, or ignore.")
    lines.append("")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════
# Feedback Processing (Poll Telegram for reactions/replies)
# ══════════════════════════════════════════════════════════════════════

LAST_UPDATE_ID_FILE = os.path.join(os.path.expanduser("~"), ".hermes", "data", "suggest-update-offset")


def get_last_update_offset():
    """Get the last processed update_id offset."""
    try:
        with open(LAST_UPDATE_ID_FILE) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError, OSError):
        return 0


def save_last_update_offset(offset):
    """Save the last processed update_id."""
    os.makedirs(os.path.dirname(LAST_UPDATE_ID_FILE), exist_ok=True)
    try:
        with open(LAST_UPDATE_ID_FILE, "w") as f:
            f.write(str(offset))
    except OSError:
        pass


def process_feedback():
    """Poll Telegram for reactions and command replies to suggestion messages.
    
    Processes: 👍 (accept), 👎 (dismiss), /done commands.
    Stores feedback in suggestions_log table.
    """
    token = _read_token()
    if not token:
        return
    
    offset = get_last_update_offset()
    
    # Build params for getUpdates
    params = {"timeout": 10, "limit": 50}
    if offset > 0:
        params["offset"] = offset

    updates = _api_call(token, "getUpdates", params)
    if not updates:
        return

    conn = get_db()
    max_update_id = offset

    for update in updates:
        update_id = update.get("update_id", 0)
        max_update_id = max(max_update_id, update_id)

        # Process message reactions (Telegram reaction updates)
        message_reaction = update.get("message_reaction")
        if message_reaction:
            chat_id = message_reaction.get("chat", {}).get("id")
            if chat_id != CHAT_ID:
                continue
            
            new_reaction = message_reaction.get("new_reaction", [])
            if new_reaction:
                emoji = new_reaction[0].get("emoji", "")
                # Find the most recent pending suggestion and update its status
                if emoji == "👍":
                    suggestion = conn.execute(
                        "SELECT id, activity_name FROM suggestions_log WHERE status='pending' "
                        "ORDER BY suggested_at DESC LIMIT 1"
                    ).fetchone()
                    if suggestion:
                        update_suggestion_status(suggestion["id"], "accepted")
                        print(f"  ✓ Feedback: '{suggestion['activity_name']}' ACCEPTED (👍)", file=sys.stderr)
                elif emoji == "👎":
                    suggestion = conn.execute(
                        "SELECT id, activity_name FROM suggestions_log WHERE status='pending' "
                        "ORDER BY suggested_at DESC LIMIT 1"
                    ).fetchone()
                    if suggestion:
                        update_suggestion_status(suggestion["id"], "dismissed")
                        print(f"  ✓ Feedback: '{suggestion['activity_name']}' DISMISSED (👎)", file=sys.stderr)

        # Process text replies (commands like /done)
        message = update.get("message") or update.get("edited_message")
        if not message:
            continue

        msg_chat_id = message.get("chat", {}).get("id")
        if msg_chat_id != CHAT_ID:
            continue

        text = (message.get("text") or "").strip()
        if not text:
            continue

        # Handle /done command
        if text.startswith("/done"):
            activity_text = text[5:].strip()
            if activity_text:
                # Try to fuzzy match against recently suggested activities
                recent = conn.execute(
                    "SELECT id, activity_name FROM suggestions_log "
                    "WHERE status='pending' "
                    "ORDER BY suggested_at DESC LIMIT 5"
                ).fetchall()
                matched = None
                for r in recent:
                    if activity_text.lower() in r["activity_name"].lower():
                        matched = r
                        break
                if matched:
                    update_suggestion_status(matched["id"], "accepted", note="via /done command")
                    print(f"  ✓ Feedback: '{matched['activity_name']}' ACCEPTED (/done)", file=sys.stderr)
                else:
                    print(f"  ? Could not match /done '{activity_text}' to any pending suggestion", file=sys.stderr)
            continue

        # Simple text reply (acceptance)
        if text.lower() in ("yes", "ok", "okay", "sure", "y", "do it", "doing"):
            suggestion = conn.execute(
                "SELECT id, activity_name FROM suggestions_log WHERE status='pending' "
                "ORDER BY suggested_at DESC LIMIT 1"
            ).fetchone()
            if suggestion:
                update_suggestion_status(suggestion["id"], "accepted")
                print(f"  ✓ Feedback: '{suggestion['activity_name']}' ACCEPTED (text reply)", file=sys.stderr)

    conn.close()

    if max_update_id > offset:
        save_last_update_offset(max_update_id + 1)

    print(f"  ✓ Processed feedback up to update_id={max_update_id}", file=sys.stderr)


# ══════════════════════════════════════════════════════════════════════
# Dashboard Note Update
# ══════════════════════════════════════════════════════════════════════

def update_vault_dashboard(result):
    """Update the Obsidian vault dashboard note with current suggestions."""
    suggestions = result["suggestions"]
    state = result["state"]
    window = result["window"]
    window_config = DELIVERY_WINDOWS[window]
    
    now = datetime.now()
    day_name = state["day_of_week"]
    hour = state["current_hour"]
    energy = state["physical_energy"]
    
    # Build dashboard content
    lines = [
        "---",
        f"updated: {now.isoformat()}",
        "type: dashboard-widget",
        f"window: {window}",
        "---",
        "",
        f"## 🎯 Right Now — {day_name} {hour}:00",
        f"Energy: {energy} · {window_config['label']}",
        "",
    ]
    
    for i, sug in enumerate(suggestions, 1):
        row = sug["row"]
        name = row["name"]
        min_t = row["min_time"] or 0
        max_t = row["max_time"] or 240
        ds = sug["days_since"]
        
        recency = "new" if ds is None else f"{ds}d"
        lines.append(f"- **{name}** ({min_t}–{max_t}min, last: {recency})")
        if row["next_action"]:
            lines.append(f"  - Next: {row['next_action']}")
    
    lines.append("")
    lines.append("---")
    lines.append(f"_Auto-generated by Activity Suggester at {now.strftime('%H:%M')}_")
    
    content = "\n".join(lines)
    
    # Write to vault dashboard
    vault_root = os.environ.get("OBSIDIAN_VAULT_PATH", os.path.expanduser("~/vaults/Projects"))
    dashboard_path = os.path.join(vault_root, "Projects/Hub/Dashboard.md")
    
    try:
        # Check if file exists
        if os.path.exists(dashboard_path):
            with open(dashboard_path) as f:
                existing = f.read()
            # Replace the Right Now section
            import re as _re
            pattern = r"## 🎯 Right Now.*?(?=## |\Z)"
            replacement = content
            # If the section exists, replace it; otherwise append
            if _re.search(pattern, existing, _re.DOTALL):
                new_content = _re.sub(pattern, content, existing, count=1, flags=_re.DOTALL)
            else:
                new_content = existing + "\n\n" + content
        else:
            os.makedirs(os.path.dirname(dashboard_path), exist_ok=True)
            new_content = content
        
        with open(dashboard_path, "w") as f:
            f.write(new_content)
        print(f"  ✓ Dashboard updated: {dashboard_path}", file=sys.stderr)
    except OSError as e:
        print(f"  ✗ Could not update dashboard: {e}", file=sys.stderr)


# ══════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════

def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Daily Activity Suggester with Telegram delivery")
    parser.add_argument("--dry-run", action="store_true", help="Print suggestions without sending")
    parser.add_argument("--window", choices=list(DELIVERY_WINDOWS.keys()), default=None,
                        help="Override delivery window detection")
    parser.add_argument("--count", type=int, default=None, help="Override suggestion count")
    parser.add_argument("--feedback-only", action="store_true",
                        help="Only process feedback, don't generate new suggestions")
    parser.add_argument("--dashboard", action="store_true", default=True,
                        help="Update vault dashboard note (default: True)")
    parser.add_argument("--no-dashboard", action="store_true",
                        help="Skip vault dashboard update")
    
    args = parser.parse_args()
    
    # Ensure suggestions table exists
    ensure_suggestions_table()
    
    # Process feedback first (skip in dry-run mode to avoid Telegram API conflicts)
    if not args.dry_run:
        print("Processing feedback from Telegram...", file=sys.stderr)
        process_feedback()
    
    if args.feedback_only:
        print("Feedback-only mode. Done.", file=sys.stderr)
        return
    
    # Detect which delivery window applies
    now = datetime.now()
    day = now.strftime("%a")
    hour = now.hour
    minute = now.minute
    
    window_name = args.window
    if not window_name:
        for wname, wconfig in DELIVERY_WINDOWS.items():
            if day in wconfig["days"] and hour == wconfig["hour"] and minute < 5:
                window_name = wname
                break
    
    if not window_name:
        window_name = "after_work"  # default
    
    count = args.count or DELIVERY_WINDOWS[window_name]["count"]
    
    print(f"Building suggestions for window: {window_name} (count={count})", file=sys.stderr)
    
    result = build_suggestions(window_name, count)
    sug_list = result["suggestions"]
    
    if not sug_list:
        print("No suggestions generated.", file=sys.stderr)
        return
    
    # Format message
    if window_name == "after_work" and count == 1:
        message = format_compact_suggestions(result)
    else:
        message = format_suggestion_message(result)
    
    # Print to stdout for dry-run
    if args.dry_run:
        print("=" * 50)
        print("DRY RUN — Suggestions:")
        print("=" * 50)
        for i, sug in enumerate(sug_list, 1):
            row = sug["row"]
            print(f"  {i}. {row['name']} (score={sug['score']:.0f}, category={row['category']})")
            print(f"     days_since={sug['days_since']}, reasons={sug['reasons']}")
        print()
        print("Message that would be sent:")
        print("-" * 40)
        print(message)
        print("-" * 40)
    else:
        # Send to Telegram
        print("Sending to Telegram...", file=sys.stderr)
        msg_id = send_message(message)
        if msg_id:
            print(f"  ✓ Sent! message_id={msg_id}", file=sys.stderr)
            
            # Also send the raw suggestion data to the ticketing system for logging
            summary = f"Activity suggestions delivered ({window_name}): {', '.join(s['row']['name'] for s in sug_list[:3])}"
            try:
                req = Request(
                    "http://127.0.0.1:5200/api/v1/messages",
                    data=json.dumps({
                        "source": "activity-suggest",
                        "content": summary,
                        "severity": "info",
                        "category": "general",
                    }).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                urlopen(req, timeout=5)
            except (URLError, OSError):
                pass  # ticketing system may not be running
        
        # Update dashboard
        update_dashboard = args.dashboard and not args.no_dashboard
        if update_dashboard:
            update_vault_dashboard(result)
    
    print("Done.", file=sys.stderr)


if __name__ == "__main__":
    main()
