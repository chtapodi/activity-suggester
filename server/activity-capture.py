#!/usr/bin/env python3
"""activity-capture.py — Quick-capture CLI for logging completed activities.

Phase 3 of T-056: Recency tracking.
Logs completed activities with timestamp, writes back to vault notes,
and provides recency decay scoring utilities.

Usage:
  python3 activity-capture.py capture --name "Archery" [--duration 90] [--at "2026-06-16T14:30"]
  python3 activity-capture.py capture --name "Archery" --duration 90 --note "Great session, hit new PB"
  python3 activity-capture.py recent [--days 7]
  python3 activity-capture.py score --name "Archery"
"""

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timezone

# ── Paths ───────────────────────────────────────────────────────────
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS_DIR)

# Reuse the same DB and constants as activity-suggest.py
CACHE_DIR = os.path.expanduser("~/.hermes/data")
CACHE_DB = os.path.join(CACHE_DIR, "activity-cache.db")

# Vault path for write-back — uses same convention as activity-suggest.py
VAULT_ROOT = os.environ.get(
    "OBSIDIAN_VAULT_PATH",
    os.path.expanduser("~/vaults/Projects")
)
DATA_DIR = "Areas/Build/Code/Activity Suggester/Data"

# Decay rates (mirrored from activity-suggest.py)
DECAY_RATES = {
    "daily": 0.30, "2-3x-week": 0.15, "weekly": 0.08,
    "biweekly": 0.04, "monthly": 0.02, "seasonal": 0.005,
}

DEFAULT_HALF_LIFE_DAYS = 7  # default half-life in days for configurable decay


# ── Database ────────────────────────────────────────────────────────
def get_db():
    """Connect to activity cache DB."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    conn = sqlite3.connect(CACHE_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def find_activity(conn, name_fragment):
    """Fuzzy-match an activity name. Returns row or None."""
    row = conn.execute(
        "SELECT * FROM activities WHERE name = ?", (name_fragment,)
    ).fetchone()
    if row:
        return dict(row)

    # Fuzzy match
    rows = conn.execute(
        "SELECT * FROM activities WHERE name LIKE ?",
        (f"%{name_fragment}%",)
    ).fetchall()
    if len(rows) == 1:
        return dict(rows[0])
    elif len(rows) > 1:
        # Try exact substring match first
        for r in rows:
            if name_fragment.lower() in r["name"].lower():
                return dict(r)
        return None  # ambiguous
    return None


# ── Vault Write-Back ────────────────────────────────────────────────
def vault_path_for_activity(row):
    """Get the vault path for an activity from its vault_path field or by lookup."""
    vp = row.get("vault_path")
    if vp and os.path.exists(vp):
        return vp

    # Try to find it by searching the vault
    name = row["name"]
    activities_glob = os.path.join(VAULT_ROOT, "Areas", "Life", "Activities", "**", "*.md")

    import glob as _glob
    for fp in sorted(_glob.glob(activities_glob, recursive=True)):
        base = os.path.splitext(os.path.basename(fp))[0]
        if base == name:
            return fp
    return None


def read_vault_note_fm(filepath):
    """Read simple YAML frontmatter from a vault note."""
    try:
        with open(filepath) as f:
            content = f.read()
    except (IOError, UnicodeDecodeError):
        return None, ""

    if not content.startswith("---"):
        return None, content

    end = content.find("---", 3)
    if end == -1:
        return None, content

    fm_text = content[3:end].strip()
    body = content[end + 3:]
    return fm_text, body


def update_vault_note_frontmatter(filepath, updates):
    """Update frontmatter fields in a vault markdown note.

    Uses simple line-based YAML parsing compatible with Obsidian.
    Returns True on success, False on failure.
    """
    fm_text, body = read_vault_note_fm(filepath)
    if fm_text is None:
        print(f"  ⚠️  No frontmatter found in {filepath}, skipping vault write-back")
        return False

    # Parse existing frontmatter into lines
    lines = fm_text.split("\n")
    updated_keys = set()

    # Process updates
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if ":" in stripped and not stripped.startswith("#") and not stripped.startswith("-"):
            key = stripped.split(":", 1)[0].strip()
            if key in updates:
                val = updates[key]
                if val is None:
                    new_lines.append(f"{key}:")
                elif isinstance(val, bool):
                    new_lines.append(f"{key}: {'true' if val else 'false'}")
                elif isinstance(val, int):
                    new_lines.append(f"{key}: {val}")
                else:
                    new_lines.append(f"{key}: {val}")
                updated_keys.add(key)
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)

    # Add any new keys that weren't in the frontmatter
    for key, val in updates.items():
        if key not in updated_keys:
            if val is None:
                new_lines.append(f"{key}:")
            elif isinstance(val, bool):
                new_lines.append(f"{key}: {'true' if val else 'false'}")
            elif isinstance(val, int):
                new_lines.append(f"{key}: {val}")
            else:
                new_lines.append(f"{key}: {val}")
            updated_keys.add(key)

    new_fm = "\n".join(new_lines)
    new_content = f"---\n{new_fm}\n---{body}"

    try:
        with open(filepath, "w") as f:
            f.write(new_content)
        print(f"  📝 Updated vault note: {os.path.relpath(filepath, VAULT_ROOT)}")
        for k in updates:
            print(f"     {k}: {updates[k]}")
        return True
    except IOError as e:
        print(f"  ⚠️  Could not write to vault: {e}")
        return False


def write_structured_record(name, duration, timestamp, note):
    """Write a structured record to the vault Data directory.

    Format: JSON records appended to a per-month file.
    """
    dt = datetime.fromisoformat(timestamp) if isinstance(timestamp, str) else timestamp
    month_key = dt.strftime("%Y-%m")
    record_path = os.path.join(VAULT_ROOT, DATA_DIR)
    os.makedirs(record_path, exist_ok=True)

    record_file = os.path.join(record_path, f"activity-log-{month_key}.json")

    entry = {
        "activity": name,
        "timestamp": dt.isoformat(),
        "duration_minutes": duration,
        "note": note or "",
        "logged_at": datetime.now(timezone.utc).isoformat(),
    }

    records = []
    if os.path.exists(record_file):
        try:
            with open(record_file) as f:
                records = json.load(f)
        except (json.JSONDecodeError, IOError):
            records = []

    records.append(entry)

    try:
        with open(record_file, "w") as f:
            json.dump(records, f, indent=2)
        print(f"  📊 Wrote structured record: {os.path.relpath(record_file, VAULT_ROOT)}")
        return True
    except IOError as e:
        print(f"  ⚠️  Could not write structured record: {e}")
        return False


# ── Recency Scoring ─────────────────────────────────────────────────
def compute_recency_score(base_interest, recency_ideal, days_since, half_life_days=None):
    """Compute exponential decay recency score.

    Uses half-life-based decay if half_life_days is provided,
    otherwise uses per-frequency decay rates.
    """
    if half_life_days:
        # Configurable half-life decay: score = base * 0.5^(days / half_life)
        return base_interest * math.pow(0.5, days_since / half_life_days)
    else:
        # Per-frequency decay rates
        dr = DECAY_RATES.get(recency_ideal or "weekly", 0.08)
        return base_interest * math.exp(-dr * min(days_since, 365))


# ── CLI Commands ────────────────────────────────────────────────────

def cmd_capture(args):
    """Capture a completed activity: log to DB, write back to vault."""
    conn = get_db()

    # Find activity
    row = find_activity(conn, args.name)
    if not row:
        print(f"❌ Activity not found: '{args.name}'")
        # Show similar names
        similar = conn.execute(
            "SELECT name FROM activities WHERE name LIKE ? LIMIT 5",
            (f"%{args.name}%",)
        ).fetchall()
        if similar:
            print("   Did you mean?")
            for s in similar:
                print(f"     - {s[0]}")
        conn.close()
        return

    name = row["name"]
    print(f"✅ Found activity: {name}")

    # Determine timestamp
    if args.at:
        try:
            ts = datetime.fromisoformat(args.at)
        except ValueError:
            print(f"  ⚠️  Invalid timestamp format: {args.at}. Using current time.")
            ts = datetime.now()
    else:
        ts = datetime.now()

    ts_str = ts.isoformat()

    # 1. Log to recency_log table
    conn.execute(
        "INSERT INTO recency_log (activity_name, done_at, duration_minutes) VALUES (?, ?, ?)",
        (name, ts_str, args.duration)
    )
    conn.commit()

    # Get updated count
    count = conn.execute(
        "SELECT COUNT(*) FROM recency_log WHERE activity_name=?",
        (name,)
    ).fetchone()[0]

    print(f"  💾 Logged to cache DB: {name}")
    if args.duration:
        print(f"     Duration: {args.duration} min")
    print(f"     Times done: {count}")
    if args.at:
        print(f"     When: {ts_str}")

    # 2. Vault write-back — update activity note frontmatter
    vault_path = vault_path_for_activity(row)
    if vault_path:
        old_count = row.get("times_done", 0) or 0
        update_vault_note_frontmatter(vault_path, {
            "last_done": ts_str,
            "times_done": old_count + 1 if old_count is not None else 1,
        })
    else:
        print(f"  ⚠️  No vault note found for '{name}', skipping vault write-back")

    # 3. Write structured record to Data directory
    write_structured_record(name, args.duration, ts, args.note)

    # Show recency info
    ideal = row.get("recency_ideal") or "weekly"
    base_i = row.get("base_interest") or 5
    print(f"\n  📊 Recency profile: ideal={ideal}, base_interest={base_i}")

    # All-time stats
    all_count = conn.execute(
        "SELECT COUNT(*) FROM recency_log WHERE activity_name=?",
        (name,)
    ).fetchone()[0]
    last_log = conn.execute(
        "SELECT MAX(done_at) FROM recency_log WHERE activity_name=?",
        (name,)
    ).fetchone()[0]
    print(f"  🏆 All-time: {all_count}x, last={last_log[:10] if last_log else 'never'}")

    conn.close()


def cmd_recent(args):
    """Show recent activity log entries."""
    conn = get_db()

    days = args.days or 7

    rows = conn.execute("""
        SELECT activity_name, done_at, duration_minutes
        FROM recency_log
        WHERE done_at >= datetime('now', ?)
        ORDER BY done_at DESC
        LIMIT 20
    """, (f"-{days} days",)).fetchall()

    print(f"\n  📋 Activity log — last {days} days\n")
    if not rows:
        print("  (no activity logged in this period)")
        conn.close()
        return

    print(f"  {'Activity':<35s} {'When':>20s}  {'Duration':>8s}")
    print(f"  {'─'*35} {'─'*20}  {'─'*8}")
    for r in rows:
        dur = f"{r[2]}m" if r[2] else "?"
        print(f"  {r[0]:<35s} {r[1]:>20s}  {dur:>8s}")

    # Summary
    total = len(rows)
    unique = len(set(r[0] for r in rows))
    print(f"\n  Total: {total} entries · {unique} unique activities")

    conn.close()


def cmd_score(args):
    """Compute recency score for an activity."""
    conn = get_db()

    row = find_activity(conn, args.name)
    if not row:
        print(f"❌ Activity not found: '{args.name}'")
        conn.close()
        return

    name = row["name"]
    base_i = row.get("base_interest") or 5
    ideal = row.get("recency_ideal") or "weekly"

    # Get recency info
    last = conn.execute(
        "SELECT MAX(done_at) FROM recency_log WHERE activity_name=?",
        (name,)
    ).fetchone()[0]

    if last:
        try:
            days_since = (date.today() - datetime.fromisoformat(last).date()).days
        except (ValueError, TypeError):
            days_since = None
    else:
        days_since = None

    count = conn.execute(
        "SELECT COUNT(*) FROM recency_log WHERE activity_name=?",
        (name,)
    ).fetchone()[0]

    print(f"\n  📊 Recency Score: {name}")
    print(f"     Base interest:    {base_i}")
    print(f"     Recency ideal:    {ideal}")
    print(f"     Times done:       {count}")
    print(f"     Last done:        {last or 'never'}")
    print(f"     Days since:       {days_since if days_since is not None else 'N/A'}")

    if days_since is not None:
        # Per-frequency decay
        decay_score = compute_recency_score(base_i, ideal, days_since)
        print(f"\n     Decay rate:       {DECAY_RATES.get(ideal, 0.08):.3f}")
        print(f"     Decay score:      {decay_score:.1f}")

        # Configurable half-life
        for hl in [3, 7, 14, 30]:
            hl_score = compute_recency_score(base_i, ideal, days_since, half_life_days=hl)
            print(f"     Half-life {hl:>2}d:     {hl_score:.1f}")

        # Neglect check
        ideal_days = {"daily": 1, "2-3x-week": 3, "weekly": 7,
                      "biweekly": 14, "monthly": 30, "seasonal": 90}
        ideal_n = ideal_days.get(ideal, 7)
        if days_since > 3 * ideal_n:
            print(f"\n     ⚠️  NEGLECTED: {days_since}d since last, ideal is every {ideal_n}d")
        elif days_since > ideal_n:
            print(f"\n     ⏰  Due: {days_since}d since last (ideal every {ideal_n}d)")
        else:
            print(f"\n     ✅  On track (ideal every {ideal_n}d)")
    else:
        print(f"\n     🆕  Never done — full interest bonus applies")

    conn.close()


# ── Main ────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Activity Capture — quick-capture CLI with vault write-back"
    )
    sp = p.add_subparsers(dest="command")

    # capture
    cap = sp.add_parser("capture", help="Log a completed activity")
    cap.add_argument("--name", required=True, help="Activity name (fuzzy match)")
    cap.add_argument("--duration", type=int, default=None, help="Minutes spent")
    cap.add_argument("--at", default=None, help="ISO timestamp (default: now)")
    cap.add_argument("--note", default=None, help="Optional note about the session")

    # recent
    rec = sp.add_parser("recent", help="Show recent activity log entries")
    rec.add_argument("--days", type=int, default=7, help="Days of history to show")

    # score
    sco = sp.add_parser("score", help="Compute recency decay score for an activity")
    sco.add_argument("--name", required=True, help="Activity name (fuzzy match)")

    args = p.parse_args()

    if not args.command:
        p.print_help()
        return

    if args.command == "capture":
        cmd_capture(args)
    elif args.command == "recent":
        cmd_recent(args)
    elif args.command == "score":
        cmd_score(args)


if __name__ == "__main__":
    main()
