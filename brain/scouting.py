"""The scouting layer — turns raw Sleeper data into decision-ready intelligence.

This is the single place that answers "what does the bot know?" so every
decision path — lineup, waivers, trade evaluation, AND DM negotiation — sees
the same complete picture instead of each one hand-rolling a thin player card.

A player card here carries real production under YOUR league's exact scoring
(season-to-date AND projected fantasy points per game), games played, position,
team, and injury/role status. Team context carries your record, your rank in
the league, the live head-to-head you're currently playing, and the standings.

Everything is best-effort: if the stats endpoint hiccups, cards simply fall
back to name/position/status (what the bot had before) and the live web search
in the brain fills the gap. A data outage never breaks a decision.
"""

from brain.scoring import score_statline, per_game, per_game_cats, games_played, explain_weights


def get_league_scoring(league: dict) -> dict:
    """Prefer the league's OWN live scoring_settings (ground truth) and fall
    back to the constant we hardcoded. This way the bot scores by whatever the
    league actually uses, even if the commissioner tweaks it mid-season."""
    from config import SCORING_WEIGHTS
    if isinstance(league, dict):
        ss = league.get("scoring_settings")
        if isinstance(ss, dict) and ss:
            return ss
    return SCORING_WEIGHTS


def _fmt_line(cats: dict) -> str:
    """Compact, readable per-game line: 'pts 24.1, reb 5.2, ast 8.0, ...'."""
    if not cats:
        return ""
    order = ["pts", "reb", "ast", "stl", "blk", "to", "tpm"]
    parts = [f"{k} {cats[k]}" for k in order if k in cats]
    return ", ".join(parts)


class Scout:
    """Loads the player DB + season stats + projections once, then builds rich
    cards and context on demand. Create one per run and reuse it."""

    def __init__(self, read, league: dict = None,
                 season_stats: dict = None, projections: dict = None,
                 weights: dict = None):
        self.read = read
        self.league = league
        self.players = read.all_players()
        self.weights = weights or get_league_scoring(league)
        # Best-effort pulls; empty dict on any failure.
        self.stats = season_stats if season_stats is not None else read.season_stats()
        self.proj = projections if projections is not None else read.season_projections()

    # ---------- player cards ----------
    def card(self, pid) -> dict:
        """A single enriched player card."""
        pid = str(pid)
        p = self.players.get(pid, {}) or {}
        name = (p.get("full_name")
                or " ".join(x for x in [p.get("first_name"), p.get("last_name")] if x).strip()
                or pid)
        card = {
            "player_id": pid,
            "name": name,
            "pos": p.get("fantasy_positions"),
            "team": p.get("team"),
            "status": p.get("injury_status") or "OK",
        }
        # Real season-to-date production under our scoring.
        st = (self.stats or {}).get(pid)
        if st:
            fpg, gp = per_game(st, self.weights)
            if gp:
                card["fpts_per_game"] = fpg
                card["games_played"] = gp
                line = _fmt_line(per_game_cats(st))
                if line:
                    card["per_game"] = line
        # Projected production (stabilizes early-season, when actuals are noisy).
        pr = (self.proj or {}).get(pid)
        if pr:
            pfpg, pgp = per_game(pr, self.weights)
            if pfpg:
                card["proj_fpts_per_game"] = pfpg
        return card

    def cards(self, pids) -> list:
        """Enriched cards for a list of player_ids, best players first, skipping
        empty '0' placeholder slots."""
        out = [self.card(p) for p in (pids or []) if p and str(p) != "0"]
        out.sort(key=lambda c: c.get("fpts_per_game", c.get("proj_fpts_per_game", 0)), reverse=True)
        return out

    def value_of(self, pid) -> float:
        """One number for a player's fantasy value — actual fpts/game if we have
        a real sample, else projection. Used to rank and to sanity-check trades."""
        c = self.card(pid)
        return c.get("fpts_per_game") or c.get("proj_fpts_per_game") or 0.0

    # ---------- free agents (for waivers) ----------
    def ranked_free_agents(self, league_id: str, limit: int = 30,
                           max_search_rank: int = 1500) -> list:
        """Available players, ranked by real fantasy value under our scoring
        rather than by Sleeper's generic search_rank."""
        owned = self.read.rostered_player_ids(league_id)
        cards = []
        for pid, p in self.players.items():
            if pid in owned or not p.get("fantasy_positions"):
                continue
            sr = p.get("search_rank")
            if sr is None or sr > max_search_rank:
                continue
            c = self.card(pid)
            # keep only players with some signal (a stat sample or a projection)
            if c.get("fpts_per_game") or c.get("proj_fpts_per_game"):
                cards.append(c)
            elif len(cards) < limit:  # fall back to search_rank order if thin
                cards.append(c)
        cards.sort(key=lambda c: c.get("fpts_per_game", c.get("proj_fpts_per_game", 0)), reverse=True)
        return cards[:limit]

    # ---------- team context ----------
    def _owner_names(self, league_id: str) -> dict:
        try:
            return {str(u.get("user_id")): u.get("display_name")
                    for u in self.read.league_users(league_id)}
        except Exception:
            return {}

    def team_context(self, league_id: str, my_roster: dict, user_id: str, week: int) -> dict:
        """Everything about the team's situation: scoring rules, record, rank,
        the live matchup it's trying to win this week, and the standings."""
        ctx = {"scoring_rules": explain_weights(), "week": week}
        names = self._owner_names(league_id)
        my_rid = my_roster.get("roster_id")

        # Record + rank
        standings = []
        try:
            standings = self.read.standings(league_id)
        except Exception:
            standings = []
        if standings:
            me = next((r for r in standings if r["roster_id"] == my_rid), None)
            if me:
                ctx["my_record"] = f'{me["wins"]}-{me["losses"]}' + (f'-{me["ties"]}' if me["ties"] else "")
                ctx["my_rank"] = f'{me["rank"]} of {len(standings)}'
                ctx["my_points_for"] = me["fpts"]
            ctx["standings"] = [
                {"rank": r["rank"],
                 "team": names.get(r["owner_id"], f'roster {r["roster_id"]}'),
                 "record": f'{r["wins"]}-{r["losses"]}',
                 "pts_for": r["fpts"]}
                for r in standings
            ]

        # Live head-to-head this week
        try:
            mu = self.read.my_matchup(league_id, week, my_rid)
        except Exception:
            mu = {}
        if mu and mu.get("opp"):
            opp = mu["opp"]
            mine = mu.get("mine", {})
            opp_roster = next((r for r in self.read.rosters(league_id)
                               if r.get("roster_id") == opp.get("roster_id")), {})
            opp_owner = names.get(str(opp_roster.get("owner_id")), f'roster {opp.get("roster_id")}')
            # opponent's most dangerous players, by our scoring
            opp_top = self.cards(opp_roster.get("players") or [])[:5]
            ctx["current_matchup"] = {
                "opponent": opp_owner,
                "my_points_this_week": round(float(mine.get("points") or 0), 2),
                "opponent_points_this_week": round(float(opp.get("points") or 0), 2),
                "opponent_top_players": [
                    {"name": c["name"], "fpts_per_game": c.get("fpts_per_game"),
                     "proj_fpts_per_game": c.get("proj_fpts_per_game")}
                    for c in opp_top
                ],
            }
        return ctx


# ---------- lineup slotting ----------
# Sleeper rejects a whole lineup with "the move you are trying to make is
# illegal" the moment ONE player sits in a slot their position can't fill (a
# guard in a forward slot, a center in a G slot, ...). The brain decides WHO
# starts; this decides WHERE each one sits, so the array we submit is always
# positionally legal. Separating the two means a dual-guard can never get
# stranded in a forward slot and kill the entire lineup write again.
SLOT_ELIGIBILITY = {
    "PG": {"PG"}, "SG": {"SG"}, "SF": {"SF"}, "PF": {"PF"}, "C": {"C"},
    "G": {"PG", "SG"}, "F": {"SF", "PF"},
    "GF": {"PG", "SG", "SF", "PF"}, "WING": {"SG", "SF"},
    "FC": {"SF", "PF", "C"},
    "UTIL": None,   # anyone
}


def _slot_allows(pos, slot) -> bool:
    allowed = SLOT_ELIGIBILITY.get(slot)
    if allowed is None:          # UTIL, or any slot code we don't recognize -> anyone
        return True
    return bool(set(pos or []) & allowed)


def assign_lineup_slots(slots, chosen_ids, pos_by_id, fill_ids=None):
    """Seat players into positional starting slots so every seat is legal.

    slots       -> ordered slot codes, e.g. ["PG","SG","G","SF","PF","F","C","UTIL","UTIL"]
    chosen_ids  -> the players the brain wants to START, in priority order
    pos_by_id   -> {player_id: [eligible positions]}
    fill_ids    -> extra rostered players (best-first) used ONLY to fill slots
                   the chosen set leaves empty, so we never submit a hole.

    Returns (starters, seated, benched, holes):
      starters -> list the length of `slots`; starters[i] is the player for
                  slots[i], or "0" if nothing legal could fill it.
      seated   -> chosen_ids that got a seat.
      benched  -> chosen_ids that couldn't be seated (brain over-picked a position).
      holes    -> slot codes left empty.

    Uses augmenting-path matching (Kuhn's), so a player eligible for several
    slots is moved around to make room for others rather than blocking them.
    Augmenting never unseats anyone, and chosen players are processed in
    priority order, so higher-priority picks always keep a seat.
    """
    n = len(slots)
    slot_of = [None] * n          # slot index -> player_id

    def augment(pid, visited):
        for si in range(n):
            if si in visited or not _slot_allows(pos_by_id.get(pid), slots[si]):
                continue
            visited.add(si)
            if slot_of[si] is None or augment(slot_of[si], visited):
                slot_of[si] = pid
                return True
        return False

    seated, benched = [], []
    for pid in chosen_ids:
        pid = str(pid)
        if not pid or pid == "0":
            continue
        (seated if augment(pid, set()) else benched).append(pid)

    # Backfill any still-empty slots with the best remaining rostered players,
    # so a dropped starter never leaves a zero-scoring hole.
    placed = set(p for p in slot_of if p is not None)
    for pid in (fill_ids or []):
        if None not in slot_of:
            break
        pid = str(pid)
        if not pid or pid == "0" or pid in placed:
            continue
        if augment(pid, set()):
            placed.add(pid)

    starters = [p if p is not None else "0" for p in slot_of]
    holes = [slots[i] for i in range(n) if slot_of[i] is None]
    return starters, seated, benched, holes


# Framework the brain shares across every decision — the "how to think" layer
# that complements the live numbers the Scout provides. Kept here so lineup,
# waivers, trades, and the DM negotiator all reason from the same playbook.
ANALYST_FRAMEWORK = """DECISION FRAMEWORK — how to read the numbers you're given:

Each player card may carry: fpts_per_game (REAL production this season under our
scoring), proj_fpts_per_game (projection — trust this more while sample sizes
are tiny early in the season), per_game (the raw stat line), games_played,
position, team, and injury status. Use these NUMBERS as the spine of every
judgment — never rank by name recognition or reputation when a real figure is
in front of you.

What actually moves fantasy value, in rough priority:
1. REST-OF-SEASON production under THIS league's scoring, weighted toward the
   last few weeks of form. A hot or cold streak that reflects a real role change
   matters; random variance does not.
2. ROLE & OPPORTUNITY. Minutes, usage, and starting role drive everything. A
   bench player who just inherited a starter's minutes (injury ahead of them,
   trade, rotation change) is the single biggest source of waiver value.
3. SCHEDULE. Games played per week is huge in NBA fantasy — a lesser player with
   4 games can out-score a star with 2. Factor it into lineups and streaming.
4. INJURY / AVAILABILITY. An injured star on the bench scores zero. Discount for
   games missed and for durability risk over a season.
5. OUR SCORING EDGE. Steals and blocks pay 2x, points only 0.5x, turnovers -1,
   double-doubles +1 and triple-doubles +2. Defensive stat-stuffers and
   line-fillers are systematically underrated by opponents who count points.
6. POSITIONAL FIT & CONSOLIDATION. Fill real holes. Two good-for-one-great
   (2-for-1) deals that upgrade a starting slot are usually worth it in a
   16-team league where the waiver wire is thin.
7. BUY-LOW / SELL-HIGH. Target players whose owner is panicking over a slump or
   a short injury; move players who are peaking above their true level.

Always convert a decision to expected fantasy points under our scoring before
committing, and say what would make you wrong."""
