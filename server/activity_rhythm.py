"""
Activity rhythm profile — maps day+hour to default energy/mental state.
Edit this file to adjust your schedule. The system uses these defaults
when --energy and --mental are not explicitly provided.

Format: list of (day_pattern, hour_range, energy, mental, notes)
  day_pattern: "weekday" | "weekend" | "Mon" | "Tue" | ... | "any"
  hour_range: (start_hour, end_hour) — inclusive start, exclusive end
  energy: "drained" | "low" | "medium" | "high"
  mental: "fried" | "tired" | "medium" | "fresh"
"""

SCHEDULE = [
    # ── Weekday work hours ──
    ("weekday", (0, 7),   "drained", "tired",   "sleeping"),
    ("weekday", (7, 9),   "medium",  "medium",   "morning routine"),
    ("weekday", (9, 12),  "medium",  "fresh",    "work — morning focus, can't do long activities"),
    ("weekday", (12, 13), "medium",  "medium",   "lunch break — quick activities possible"),
    ("weekday", (13, 17), "medium",  "medium",   "work — afternoon"),
    ("weekday", (17, 19), "low",     "tired",    "just finished work — mentally drained"),
    ("weekday", (19, 20), "low",     "tired",    "dinner time — 7:30-8pm, don't suggest activities"),
    ("weekday", (20, 22), "medium",  "medium",   "evening — moderate energy, light activities"),
    ("weekday", (22, 24), "low",     "tired",    "winding down — nothing stimulating, no screens"),

    # ── Weekend ──
    ("weekend", (0, 8),   "drained", "tired",   "sleeping in"),
    ("weekend", (8, 12),  "high",    "fresh",    "morning — peak energy, best time for projects"),
    ("weekend", (12, 17), "high",    "fresh",    "afternoon — still good, outdoor activities"),
    ("weekend", (17, 20), "medium",  "medium",   "evening — winding down"),
    ("weekend", (20, 24), "low",     "tired",    "late evening — relaxation only"),
]

# ── Time-of-day activity restrictions ──
# Activities with these tags are penalized or blocked in certain windows.
# Gates: completely blocked. Penalties: score reduction.
LATE_NIGHT_START = 22  # After 10pm
LATE_NIGHT_BLOCK_TAGS = ["deep-focus", "needs-gear", "no-interruptions"]
LATE_NIGHT_BLOCK_CATEGORIES = ["physical", "flying", "social"]
LATE_NIGHT_SCREEN_PENALTY = -8  # Heavy penalty for screen activities after 10pm

DINNER_WINDOW = (19, 20)  # 7-8pm — dinner, don't suggest anything
WORKDAY_SHORT_WINDOW = 30  # During work hours, max suggested time is 30min
WORKDAY_BLOCK_HOURS = [(9, 12), (13, 17)]  # Core work hours
