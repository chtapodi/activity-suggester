#!/usr/bin/env python3
"""ha_context.py — Live Home Assistant context for the Activity Suggester.

Queries HA REST API for real environmental state and caches results.
Designed to be imported by activity-server.py and activity-suggest.py.

Sources (what HA actually has):
  - Indoor temperature: average of Aqara motion sensor temps (3 rooms)
  - Daylight: sun_next_rising / sun_next_setting timestamps
  - Room occupancy: which rooms detected motion recently
  - at_home: default True (no device trackers configured)

Config via environment:
  HOMEASSISTANT_URL  — http://192.168.50.84:8123
  HOMEASSISTANT_TOKEN — long-lived access token
"""

import os
import time
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone


# ── Config ──────────────────────────────────────────────────────────
HA_URL = os.environ.get("HOMEASSISTANT_URL", "http://192.168.50.84:8123")
HA_TOKEN = os.environ.get("HOMEASSISTANT_TOKEN", "")
CACHE_TTL = 60  # seconds


# ── Cache ───────────────────────────────────────────────────────────
_cache = {"data": None, "fetched_at": 0}


def _ha_get(path: str) -> dict | None:
    """Call HA REST API. Returns parsed JSON or None on failure."""
    if not HA_TOKEN:
        return None
    url = f"{HA_URL}/api/{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {HA_TOKEN}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def _ha_state(entity_id: str) -> dict | None:
    """Get a single entity's state."""
    return _ha_get(f"states/{entity_id}")


# ── Public API ──────────────────────────────────────────────────────

def get_ha_context() -> dict:
    """Return live HA context dict. Cached for CACHE_TTL seconds.

    Returns keys:
        at_home: bool          — True (no device trackers)
        temperature: float|None — indoor temp in °C (avg of Aqara sensors)
        is_daylight: bool|None — True if sun is up
        occupied_rooms: list   — rooms with recent motion
        ha_available: bool     — True if HA responded
    """
    now = time.time()
    if _cache["data"] is not None and (now - _cache["fetched_at"]) < CACHE_TTL:
        return _cache["data"]

    ctx = {
        "at_home": True,          # default — no person trackers
        "temperature": None,
        "is_daylight": None,
        "occupied_rooms": [],
        "ha_available": False,
    }

    if not HA_TOKEN:
        _cache["data"] = ctx
        _cache["fetched_at"] = now
        return ctx

    # ── Indoor temperature (average of Aqara sensors) ───────────────
    temps = []
    for suffix in ("", "_2", "_3"):
        state = _ha_state(f"sensor.lumi_lumi_sensor_motion_aq2_device_temperature{suffix}")
        if state and state.get("state") not in (None, "unknown", "unavailable"):
            try:
                temps.append(float(state["state"]))
            except (ValueError, TypeError):
                pass
    if temps:
        ctx["temperature"] = round(sum(temps) / len(temps), 1)
        ctx["ha_available"] = True

    # ── Daylight (sun position) ─────────────────────────────────────
    rising = _ha_state("sensor.sun_next_rising")
    setting = _ha_state("sensor.sun_next_setting")
    if rising and setting:
        try:
            rise = datetime.fromisoformat(rising["state"])
            set_ = datetime.fromisoformat(setting["state"])
            now_utc = datetime.now(timezone.utc)
            ctx["is_daylight"] = rise <= now_utc <= set_
            ctx["ha_available"] = True
        except (ValueError, TypeError):
            pass

    # ── Room occupancy (motion sensors) ─────────────────────────────
    room_sensors = {
        "kitchen": "binary_sensor.lumi_lumi_sensor_motion_aq2",
        "bathroom": "binary_sensor.lumi_lumi_sensor_motion_aq2_2",
        "laundry": "binary_sensor.lumi_lumi_sensor_motion_aq2_3",
    }
    for room, entity_id in room_sensors.items():
        state = _ha_state(entity_id)
        if state and state.get("state") == "on":
            ctx["occupied_rooms"].append(room)
    if ctx["occupied_rooms"]:
        ctx["ha_available"] = True

    _cache["data"] = ctx
    _cache["fetched_at"] = now
    return ctx


def get_temperature() -> float | None:
    """Convenience: just the indoor temperature."""
    return get_ha_context()["temperature"]


def get_daylight() -> bool | None:
    """Convenience: is the sun up?"""
    return get_ha_context()["is_daylight"]


def get_occupied_rooms() -> list:
    """Convenience: rooms with recent motion."""
    return get_ha_context()["occupied_rooms"]


# ── CLI (for testing) ───────────────────────────────────────────────
if __name__ == "__main__":
    ctx = get_ha_context()
    print(json.dumps(ctx, indent=2, default=str))
