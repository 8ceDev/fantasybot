"""Sleeper READ client.

This talks only to the public, documented, read-only API at
https://api.sleeper.app/v1 — no auth required. Everything here is safe and
officially supported. Docs: https://docs.sleeper.app

Sleeper asks you to stay under 1000 calls/minute. We also cache the giant
player dump to disk because it's ~5MB and only changes daily.
"""

import json
import os
import time
import requests

BASE = "https://api.sleeper.app/v1"
PLAYER_CACHE = os.path.join(os.path.dirname(__file__), "..", "data", "players_nba.json")
PLAYER_CACHE_MAX_AGE = 60 * 60 * 20  # refresh player dump ~once a day


class SleeperRead:
    def __init__(self, sport: str = "nba", season: str = "2026"):
        self.sport = sport
        self.season = season
        self._session = requests.Session()

    def _get(self, path: str):
        r = self._session.get(f"{BASE}/{path}", timeout=20)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        if not r.text:
            return None
        return r.json()

    # ---- identity ----
    def user(self, username: str):
        return self._get(f"user/{username}")

    def user_leagues(self, user_id: str):
        return self._get(f"user/{user_id}/leagues/{self.sport}/{self.season}") or []

    # ---- league ----
    def league(self, league_id: str):
        return self._get(f"league/{league_id}")

    def rosters(self, league_id: str):
        return self._get(f"league/{league_id}/rosters") or []

    def league_users(self, league_id: str):
        return self._get(f"league/{league_id}/users") or []

    def matchups(self, league_id: str, week: int):
        return self._get(f"league/{league_id}/matchups/{week}") or []

    def transactions(self, league_id: str, week: int):
        return self._get(f"league/{league_id}/transactions/{week}") or []

    def traded_picks(self, league_id: str):
        return self._get(f"league/{league_id}/traded_picks") or []

    # ---- draft ----
    def draft(self, draft_id: str):
        return self._get(f"draft/{draft_id}")

    def draft_picks(self, draft_id: str):
        return self._get(f"draft/{draft_id}/picks") or []

    # ---- state ----
    def state(self):
        return self._get(f"state/{self.sport}")

    # ---- players ----
    def trending(self, kind: str = "add", hours: int = 24, limit: int = 50):
        """kind = 'add' or 'drop'. Returns list of {player_id, count}."""
        return self._get(f"players/{self.sport}/trending/{kind}?lookback_hours={hours}&limit={limit}") or []

    def all_players(self, force_refresh: bool = False) -> dict:
        """The full player database keyed by player_id. Cached to disk."""
        if not force_refresh and os.path.exists(PLAYER_CACHE):
            age = time.time() - os.path.getmtime(PLAYER_CACHE)
            if age < PLAYER_CACHE_MAX_AGE:
                with open(PLAYER_CACHE) as f:
                    return json.load(f)
        data = self._get(f"players/{self.sport}") or {}
        os.makedirs(os.path.dirname(PLAYER_CACHE), exist_ok=True)
        with open(PLAYER_CACHE, "w") as f:
            json.dump(data, f)
        return data

    # ---- convenience ----
    def my_roster(self, league_id: str, user_id: str):
        for r in self.rosters(league_id):
            if r.get("owner_id") == user_id:
                return r
        return None

    def rostered_player_ids(self, league_id: str) -> set:
        """Every player currently owned by anyone — the inverse is free agents."""
        owned = set()
        for r in self.rosters(league_id):
            for pid in (r.get("players") or []):
                owned.add(pid)
        return owned
