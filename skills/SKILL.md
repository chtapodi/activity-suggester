---
name: activity-suggester
description: "Conversational interface to the Activity Suggester system. Use when the user asks what they should do, wants to log an activity, check their stats, or browse options. Handles 147 activities across hobbies, projects, chores, and relaxation — with rhythm-aware scheduling, weather gates, and recency tracking."
version: 1.0.0
author: Hermes Agent
metadata:
  hermes:
    tags: [activity, suggestion, hobby, chore, productivity, lifestyle]
    related_skills: []
---

# Activity Suggester — Hermes Interface

The user has a system that tracks ~147 activities (hobbies, code projects, chores, relaxation) with constraint filtering and recency tracking. You are the conversational frontend to this system.

## System Architecture

The activity suggester has three interfaces sharing one backend:

### Web Interface (primary visual)
`http://localhost:8092` (or `http://192.168.50.84:8092` from other devices)

A single-page app with constraint-first filtering. The user narrows down by answering constraint questions (time, energy, mental, location, intent) via filter chips in a left sidebar. The main panel shows a **D3.js zoomable sunburst chart** — 3 concentric rings (intents→categories→activities), color-coded by intent, click to drill down. Stats tab has a bubble chart (recency × interest). Dark parchment theme throughout.

**Server:** Flask at `~/.hermes/scripts/activity-server.py`, port 8092, systemd auto-start.
**Endpoints:** `/api/v1/filters`, `/api/v1/activities`, `/api/v1/log`, `/api/v1/stats`, `/api/v1/surprise`, `/api/v1/state`.
**Firewall:** Port 8092 requires `sudo ufw allow 8092/tcp` for network access.
**Setup details:** See `references/web-interface.md`.

### Hermes Conversation (you)
Use the CLI for suggestions, logging, and stats. The web UI is for visual browsing — don't try to replicate it in text. When the user wants to "see options" or "browse," point them to the web interface.

### CLI (programmatic)
`~/.hermes/scripts/activity-suggest.py`
- `suggest --json` — ranked list of activities passing constraints (use this, not `browse`)
- `browse` — interactive CLI tree (don't use in conversation; use suggest --json instead)
- `done --done "<name>"` — log completed activity
- `stats --days N` — recency history and neglected items
- `rebuild` — force cache rebuild from vault

### Data & Rhythm
**Cache:** SQLite at `~/.hermes/data/activity-cache.db` (auto-rebuilds when vault changes). 147 activities: 76 static + 39 auto-discovered projects + 32 tasks from vault checkboxes.
**Rhythm:** `~/.hermes/scripts/activity_rhythm.py` — maps day+hour to energy/mental defaults. Weekdays: work hours (9-12,13-17) restrict to ≤30min activities, dinner (19-20) blocks everything, post-work (17-19) sets energy=low/mental=tired, late-night (22+) blocks stimulating/physical/outdoor. Weekends: morning peak (8-12) fresh/high, afternoon (12-17) fresh/high, evening moderate.
**Recency:** `recency_log` table tracks every logged activity with timestamps. Decay scoring penalizes recently-done activities and boosts neglected ones.

---

## ⚠️ Anti-Prescriptive — The Cardinal Rule

**The user HATES being told what to do.** Never present ranked lists with "you should do X" or "must/should/want" prescriptions. Never say "Here's what you need to do" or "Your top priority is..."

The user wants to EXPLORE and DECIDE, not be assigned homework. The system should help them narrow down options through filters and constraints, then let them pick. It's a browsing tool, not a taskmaster.

**Wrong (prescriptive — do NOT do this):**
```
You should do: 1. Clean toilets (14d overdue), 2. Read HA sensors, 3. Cocktail Creation
```

**Right (interactive — DO this):**
```
With 60 minutes and you're tired, 41 activities match. Want me to narrow by category?
Or pick one at random?
```

**Right (simple list with quick actions):**
```
Top matches for low energy + 60min:
- Mushroom Cultivation (15-60m, hands-on, you like this)
- Light Reading (any duration, no setup)
- Meditation (5-45m, always restorative)
Want me to narrow further or pick one for you?
```

Key phrases that signal the user wants exploration, not prescription:
- "what can I do" / "show me options" / "what's available"
- "I don't know" / "surprise me" / "something different"
- "what's good right now" / "what have I been neglecting"

## Constraint-First Interaction Model

Don't navigate a taxonomy tree (Make → Code → Activity). Let the user narrow down by answering constraint questions. Each filter chip reduces the pool. The user drives; the system responds.

**Always intercept when the user says any of these:**
- "what should I do" / "what can I do" / "I'm bored" / "I don't know what to do"
- "suggest something" / "give me options" / "what's good right now"
- "I just did X" / "I finished X" / "log X" / "I completed X"
- "what have I done this week" / "what am I neglecting" / "show my stats"
- "what's available this weekend" / "plan my Saturday"
- "I'm tired, what's easy" / "I have 30 minutes" / "something quick"

**Do NOT intercept when the user is asking about the system itself** (config, how it works, bugs).

---

## Command Patterns

### 1. User wants suggestions

Detect state from conversation:
- If user mentions time ("30 minutes", "an hour") → set `--time`
- If user mentions energy ("tired", "exhausted", "wired", "energetic") → set `--energy`
- If user mentions mental state ("can't focus", "fresh", "brain dead") → set `--mental`
- If time of day / day of week is relevant → use `--hour` and `--day` overrides
- If user mentions weather → set `--weather`
- If user mentions being out / away → set `--away`
- Otherwise: let the rhythm profile provide defaults

**Run:**
```bash
python3 ~/.hermes/scripts/activity-suggest.py suggest \
  --time <minutes> --energy <state> --mental <state> \
  --weather <state> [--away] [--day <Day>] [--hour <H>] \
  --count 8 --json
```

Parse the JSON output. Extract:
- `state` — what constraints were used
- `suggestions[]` — ranked list with name, category, score, min_time, is_project, is_chore

**Present conversationally — NEVER dump raw JSON or the CLI table.**

Good response pattern:
```
With about an hour and you're tired, the top options are:

Meditation (5-45m) — always good when your brain is fried
Mushroom Cultivation (15-60m) — low-key, hands-on, you like this  
Light Reading — perfect for tired brain, any duration

Also worth noting: Clean toilets is 14 days overdue (15min) if you want
to knock out a chore.

Want me to narrow by category? Or pick one for you?
```

**Key rules for presenting:**
- Max 4-5 suggestions. Don't overwhelm.
- Always mention one "quick win" chore if it's overdue
- Always include one relaxation option when energy is low
- If it's late night, note that explicitly ("since it's 10pm, I filtered out stimulating stuff")
- If the user seems indecisive, offer to pick one for them
- Use the rhythm to contextualize ("you're usually tired at this time, so I focused on lighter options")
- **NEVER be prescriptive.** Don't say "you should do X" or "here's what you need to do." Present options and let the user decide. The system is for exploration, not assignment.
- **When the user wants to browse visually**, point them to `http://localhost:8092` (or `http://192.168.50.84:8092` from other devices). The web interface has a constraint-first filter panel (time, energy, location, intent) and a D3.js graph visualization. Don't dump JSON — the web UI is the primary visual interface.
- **The web UI uses constraint-first filtering, not category navigation.** The user narrows down by answering constraint questions (how much time, energy level, location, what kind), NOT by browsing a taxonomy tree. If asked to "show everything" or "let me browse," direct to the web interface, not a CLI dump.

### 2. User logged an activity

**Run:**
```bash
python3 ~/.hermes/scripts/activity-suggest.py done --done "<name>" [--duration <mins>]
```

The `--done` flag does fuzzy matching on activity names. Confirm the match was correct.

**Response pattern:**
```
Logged: Archery (90 min). That's 4 times total — streak of 3 weeks in a row.
Nice.
```

If fuzzy match returns wrong activity, try again with a more specific name or ask the user.

### 3. User wants stats / history

**Run:**
```bash
python3 ~/.hermes/scripts/activity-suggest.py stats --days <N>
```

**Response pattern:**
```
This week you've done: Archery (2x), Code projects (5x), Cooking (3x)

You're neglecting: Yoga (21 days), Journaling (14 days), Budgeting (months)
```

Keep it brief. Don't list everything — highlight the top 3-5 done and top 3 neglected.

### 4. User wants weekend planning

Use `--day Sat --hour 9 --time 240` for Saturday morning. Then `--day Sun --hour 9 --time 240` for Sunday.

Mention Saturday-only activities (Farmers Market happens Sat 8-1).

**Response pattern:**
```
Saturday: 72°F, clear, calm wind — great day for outdoors.

🥇 Archery (90-180m) — range open, perfect conditions
🥈 FPV Flying (60-180m) — calm wind, good light
🥉 Farmers Market (Sat 8-1) — get produce for the week

Sunday: Good for focused projects or rest.
```

### 5. User wants to browse / "show everything"

If they truly want to browse the full tree, tell them to run `activity-suggest browse` in their terminal. Don't try to replicate the interactive tree in conversation — it's too verbose.

---

## State Detection Shortcuts

Instead of asking the user for every constraint, infer what you can:

| User says | You set |
|-----------|---------|
| "after work" / "just finished" / "long day" | `--energy low --mental tired` |
| "morning" / "fresh" / "coffee" | `--energy medium --mental fresh` |
| "weekend" / "Saturday" / "day off" | `--day Sat --hour 9 --time 240` |
| "raining" / "stuck inside" | `--weather rain` |
| "quick" / "a few minutes" / "short break" | `--time 30` |
| "I'm out" / "not home" / "on my phone outside" | `--away` |
| "before bed" / "late" / "winding down" | `--hour 22` |
| "during lunch" / "lunch break" | `--time 30 --hour 12` |

When uncertain, use the rhythm defaults (no flags) and note what was assumed.

---

## Pitfalls

1. **Don't run `browse` in conversation.** The interactive tree requires stdin interaction. Use `suggest --json` instead.
2. **Don't run `rebuild` unless asked.** The cache auto-rebuilds. Only rebuild if the user says "refresh" or "update the cache."
3. **Cache path:** `~/.hermes/data/activity-cache.db`. If it's missing or empty, run `rebuild` first.
4. **Rhythm overrides:** The rhythm profile at `~/.hermes/scripts/activity_rhythm.py` provides energy/mental defaults. User's explicit flags override. Don't override the rhythm unless the user says something that contradicts it.
5. **Late night:** After 10pm, the system automatically blocks stimulating activities (deep-focus, physical, outdoor, social, screen-heavy). Mention this when relevant.
6. **Dinner window:** Weekdays 7-8pm, nothing passes gates. If the user asks during this window, say "It's dinner time — I'd suggest focusing on that. After 8pm I can find you something."
7. **Work hours:** Weekdays 9-12 and 13-17, only activities ≤30min are shown. Don't suggest deep-focus projects during work hours.
8. **The web interface** at `http://localhost:8092` is the primary visual interface. If the user asks "show me" or "I want to see my options," point them there rather than dumping JSON.
9. **sqlite3.Row limitation:** When accessing database rows directly, use bracket access (`row["name"]`) not `.get()`. sqlite3.Row objects don't have a `.get()` method. This applies to any code touching the cache.
10. **patch tool failures on large strings:** The `patch` tool with `mode='replace'` frequently fails on large old_string/new_string pairs or strings containing special characters. When you get "old_string and new_string required" errors, do NOT retry with patch — use a terminal Python script to do the replacement instead:
```bash
python3 << 'PYEOF'
with open("file.py") as f: content = f.read()
content = content.replace(old, new)
with open("file.py", "w") as f: f.write(content)
PYEOF
```
11. **User hates half-assed web interfaces.** When building visual interfaces, use proper visualization libraries (D3.js), professional CSS with design tokens, and meaningful data representations. Never ship a card grid with onclick handlers or a D3 tree with overlapping nodes. The user will call it out immediately. Think before rendering: does position convey information? Can you tell what's happening at a glance? If not, try a different approach.
12. **The user wants visual, not textual.** Indented tree tables and text lists are rejected. Use proper graphical representations (sunburst, bubble chart, treemap) that look polished. The dark parchment theme (CSS variables, gold/bronze/muted palette) is the established visual language.
