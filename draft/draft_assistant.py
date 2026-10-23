"""Live draft assistant.

Your draft hasn't happened yet (league status was 'pre_draft'). Run this on
draft night. It watches the draft in real time, and whenever it's your pick it
tells you exactly who to take and why. By default it RECOMMENDS only — during a
fast snake draft you don't want an unofficial API auto-picking and getting it
wrong. Once you trust it you can wire auto-pick in (hook is marked below).

Usage:  python run.py draft
"""

import time

from config import (SLEEPER_LEAGUE_ID, SLEEPER_USERNAME, SLEEPER_SPORT, SLEEPER_SEASON)
from sleeper.read_client import SleeperRead
from brain import claude_manager
from brain.notify import notify

POLL_SECONDS = 5


def _player_label(p: dict) -> str:
    name = p.get("full_name") or f"{p.get('first_name','')} {p.get('last_name','')}".strip()
    pos = "/".join(p.get("fantasy_positions") or [])
    team = p.get("team") or "FA"
    status = p.get("injury_status") or ""
    return f"{name} ({pos}, {team}){' ⚠'+status if status else ''}"


def _available(all_players: dict, drafted_ids: set, limit: int = 60):
    """Best available by Sleeper's own search_rank (lower = more relevant),
    filtered to the sport's real, active-ish players."""
    pool = []
    for pid, p in all_players.items():
        if pid in drafted_ids:
            continue
        if not (p.get("fantasy_positions")):
            continue
        rank = p.get("search_rank")
        if rank is None or rank > 2000:   # drop deep/irrelevant names
            continue
        pool.append((rank, pid, p))
    pool.sort(key=lambda x: x[0])
    out = []
    for rank, pid, p in pool[:limit]:
        out.append({
            "player_id": pid,
            "name": _player_label(p),
            "pos": p.get("fantasy_positions"),
            "team": p.get("team"),
            "status": p.get("injury_status") or "OK",
            "search_rank": rank,
        })
    return out


def run():
    read = SleeperRead(SLEEPER_SPORT, SLEEPER_SEASON)

    user = read.user(SLEEPER_USERNAME)
    user_id = user["user_id"]
    league = read.league(SLEEPER_LEAGUE_ID)
    draft_id = league.get("draft_id")
    if not draft_id:
        notify("No draft_id on the league yet. Is the draft scheduled?")
        return

    draft = read.draft(draft_id)
    notify(f"🏀 Draft assistant live for '{league.get('name')}'. "
           f"Draft status: {draft.get('status')}. Loading player pool…")

    all_players = read.all_players()

    # Figure out my draft slot so we can detect my turns.
    draft_order = (draft.get("draft_order") or {})          # {user_id: slot}
    slot_to_roster = (draft.get("slot_to_roster_id") or {})
    my_slot = draft_order.get(user_id)
    settings = draft.get("settings", {})
    teams = settings.get("teams", league.get("total_rosters", 16))
    rounds = settings.get("rounds", league.get("settings", {}).get("draft_rounds", 3))
    notify(f"You are draft slot {my_slot} of {teams}. Rounds: {rounds}.")

    seen_picks = 0
    announced_turn_for = None

    while True:
        draft = read.draft(draft_id)
        status = draft.get("status")
        picks = read.draft_picks(draft_id)
        drafted_ids = {pk["player_id"] for pk in picks if pk.get("player_id")}

        # announce new picks
        if len(picks) > seen_picks:
            for pk in picks[seen_picks:]:
                p = all_players.get(pk.get("player_id"), {})
                notify(f"  Pick {pk.get('pick_no')}: {_player_label(p)}")
            seen_picks = len(picks)

        if status == "complete":
            notify("✅ Draft complete. Switch to weekly autopilot (python run.py manage).")
            return

        # whose turn is it? next pick number = len(picks)+1
        next_pick_no = len(picks) + 1
        # snake order: compute slot on the clock
        rnd = (next_pick_no - 1) // teams
        idx = (next_pick_no - 1) % teams
        on_clock_slot = (idx + 1) if rnd % 2 == 0 else (teams - idx)

        if my_slot and on_clock_slot == my_slot and announced_turn_for != next_pick_no:
            announced_turn_for = next_pick_no
            my_roster_ids = [pk["player_id"] for pk in picks
                             if str(draft_order.get(user_id)) and
                             slot_to_roster.get(str(my_slot)) == pk.get("roster_id")]
            my_roster = [_player_label(all_players.get(pid, {})) for pid in my_roster_ids]
            available = _available(all_players, drafted_ids)

            notify(f"\n⏰ YOU'RE ON THE CLOCK — pick {next_pick_no} (round {rnd+1})")
            rec = claude_manager.draft_pick({
                "your_roster": my_roster,
                "available_top": available,
                "pick_number": next_pick_no,
                "rounds_left": rounds - rnd,
            })
            if rec.get("_parse_error"):
                notify(f"🤖 (couldn't parse cleanly) {rec.get('_raw','')[:500]}")
            else:
                backups = ", ".join(
                    all_players.get(b, {}).get("full_name", b) for b in rec.get("backups", [])
                )
                notify(f"🤖 DRAFT: **{rec.get('name')}**\n   Why: {rec.get('reason')}\n"
                       f"   Backups: {backups}")
                # --- AUTO-PICK HOOK (disabled) ---
                # To let the bot actually make the pick, verify the draft
                # mutation and call a write here. Left off on purpose.

        time.sleep(POLL_SECONDS)
