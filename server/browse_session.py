#!/usr/bin/env python3
"""browse_session.py — Stateful session wrapper for the interactive activity browser."""

import time
from dataclasses import dataclass, field


@dataclass
class BrowseSession:
    constraints: dict
    path: list[str] = field(default_factory=list)         # ["make", "code"]
    scope: str = "all"
    seen: set = field(default_factory=set)
    created_at: float = field(default_factory=time.time)
    _last_level_data: dict | None = field(default=None, repr=False)
    _last_scope_sizes: dict = field(default_factory=dict, repr=False)  # intent→count for skip check

    def current_level(self) -> str:
        if not self.path:
            return "intent"
        if len(self.path) == 1:
            # Check if we should skip category level
            ld = self._last_level_data
            if ld and ld.get("level_name") == "activity":
                return "activity"
            return "category"
        return "activity"

    def set_level_data(self, data: dict):
        """Store the last level data so advance() can use it for routing."""
        self._last_level_data = data

    def advance(self, choice: str) -> str:
        """
        Process a user choice. Returns action:
          'continue' — need to fetch next level
          'done' — activity logged, session complete
          'exit' — user quit
          'back' — went up a level, need to refetch
          'invalid' — choice not recognized
        """
        level = self.current_level()
        ld = self._last_level_data

        # Global commands
        if choice == 'q':
            return 'exit'
        if choice == 's':
            return 'surprise'
        if choice == 'b':
            return self.back()

        if level == "intent":
            return self._advance_intent(choice, ld)
        elif level == "category":
            return self._advance_category(choice, ld)
        elif level == "activity":
            return self._advance_activity(choice, ld)

        return 'invalid'

    def _advance_intent(self, choice, ld):
        if not ld or not ld.get("groups"):
            return 'invalid'
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(ld["groups"]):
                intent = ld["groups"][idx]["key"]
                self.path = [intent]
                self.scope = f"intent:{intent}"
                return 'continue'
        except (ValueError, IndexError):
            pass
        return 'invalid'

    def _advance_category(self, choice, ld):
        if not ld or not ld.get("groups"):
            return 'invalid'
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(ld["groups"]):
                cat = ld["groups"][idx]["key"]
                self.path.append(cat)
                self.scope = f"category:{cat}"
                return 'continue'
        except (ValueError, IndexError):
            pass
        return 'invalid'

    def _advance_activity(self, choice, ld):
        # If showing multiple activities, numbers pick from the list
        if ld and ld.get("groups") and len(ld["groups"]) > 1:
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(ld["groups"]):
                    # Selected an activity from the list
                    a = ld["groups"][idx]
                    self._selected_name = a["key"]
                    # Convert to single-activity view
                    self._last_level_data = {
                        "level_name": "activity",
                        "title": a["label"],
                        "groups": [a],
                        "total_available": 1
                    }
                    return 'detail_view'
            except (ValueError, IndexError):
                pass

        # Single activity view — do/log/tell
        if choice == '1':  # Let's do this
            return 'log_and_exit'
        if choice == '2':  # I did this (log it)
            return 'log_and_exit'
        if choice == '3':  # Tell me more
            return 'tell_more'
        return 'invalid'

    def back(self) -> str:
        if not self.path:
            return 'exit'  # back at top = exit
        self.path.pop()
        if not self.path:
            self.scope = "all"
        elif len(self.path) == 1:
            self.scope = f"intent:{self.path[0]}"
        else:
            self.scope = f"category:{self.path[-1]}"
        return 'back'

    def mark_done(self, activity_name: str):
        self.seen.add(activity_name)

    def get_selected_activity(self) -> dict | None:
        """If we're viewing a single activity, return its data."""
        ld = self._last_level_data
        if ld and ld.get("groups") and len(ld["groups"]) == 1:
            g = ld["groups"][0]
            if g.get("representative"):
                return g["representative"]
        return None

    def is_expired(self, ttl=600) -> bool:
        return time.time() - self.created_at > ttl
