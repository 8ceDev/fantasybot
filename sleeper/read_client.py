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
# Stats + projections live OFF the /v1 path, on the same host. Verified against
# the Sleeper API docs and community SDKs (lum8rjack/sleeper-go,
# SwapnikKatkoori/sleeper-api-wrapper). Same host the rest of the client uses.
STATS_BASE = "https://api.sleeper.app"
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

    def _get_abs(self, url: str):
        """GET a fully-qualified URL (for the non-/v1 stats + projections hosts)."""
        r = self._session.get(url, timeout=25)
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

    # ---- stats & projections (the real production numbers) ----
    @staticmethod
    def _normalize_stat_collection(raw) -> dict:
        """Sleeper has returned stats as either a dict keyed by player_id or a
        list of per-player objects over the years, and the statline is sometimes
        nested under a "stats" key. Normalize any of those shapes to
        {player_id: statline_dict}. Anything unexpected just yields {}."""
        out = {}
        if isinstance(raw, dict):
            for pid, v in raw.items():
                if isinstance(v, dict):
                    inner = v.get("stats")
                    out[str(pid)] = inner if isinstance(inner, dict) else v
        elif isinstance(raw, list):
            for e in raw:
                if not isinstance(e, dict):
                    continue
                pid = e.get("player_id") or e.get("playerId")
                if pid is None:
                    continue
                inner = e.get("stats")
                out[str(pid)] = inner if isinstance(inner, dict) else e
        return out

    def season_stats(self, season: str = None, season_type: str = "regular") -> dict:
        """All players' season stat TOTALS, keyed by player_id. Best-effort:
        returns {} on any failure so a stats outage never breaks a decision."""
        season = season or self.season
        url = f"{STATS_BASE}/stats/{self.sport}/{season}?season_type={season_type}"
        try:
            return self._normalize_stat_collection(self._get_abs(url))
        except Exception:
            return {}

    def season_projections(self, season: str = None, season_type: str = "regular") -> dict:
        """All players' season projection TOTALS, keyed by player_id. Best-effort."""
        season = season or self.season
        url = f"{STATS_BASE}/projections/{self.sport}/{season}?season_type={season_type}"
        try:
            return self._normalize_stat_collection(self._get_abs(url))
        except Exception:
            return {}

    # ---- matchup & standings (who I'm playing + where I stand) ----
    def my_matchup(self, league_id: str, week: int, my_roster_id: int) -> dict:
        """The current week's head-to-head: my row and my opponent's row, each
        carrying live `points`, `starters`, and `players`. Two rosters sharing a
        `matchup_id` are playing each other. Returns {} if not found."""
        rows = self.matchups(league_id, week)
        if not rows:
            return {}
        mine = next((m for m in rows if m.get("roster_id") == my_roster_id), None)
        if not mine:
            return {}
        mid = mine.get("matchup_id")
        opp = next((m for m in rows
                    if m.get("matchup_id") == mid and m.get("roster_id") != my_roster_id), None)
        return {"mine": mine, "opp": opp}

    def standings(self, league_id: str) -> list:
        """Build the standings table from rosters' settings, sorted best-first
        (wins, then total points). Each row: roster_id, owner_id, wins, losses,
        ties, fpts, fpts_against, rank."""
        table = []
        for r in self.rosters(league_id):
            s = r.get("settings", {}) or {}
            fpts = float(s.get("fpts", 0) or 0) + float(s.get("fpts_decimal", 0) or 0) / 100.0
            fpa = float(s.get("fpts_against", 0) or 0) + float(s.get("fpts_against_decimal", 0) or 0) / 100.0
            table.append({
                "roster_id": r.get("roster_id"),
                "owner_id": str(r.get("owner_id")),
                "wins": s.get("wins", 0) or 0,
                "losses": s.get("losses", 0) or 0,
                "ties": s.get("ties", 0) or 0,
                "fpts": round(fpts, 2),
                "fpts_against": round(fpa, 2),
            })
        table.sort(key=lambda x: (x["wins"], x["fpts"]), reverse=True)
        for i, row in enumerate(table, 1):
            row["rank"] = i
        return table
