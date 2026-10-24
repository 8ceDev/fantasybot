"""In-season autopilot: lineup, waivers, and trades.

Two entry points, both called from run.py and scheduled on Render:

  set_lineup()      -> daily, picks who starts today
  manage_team()     -> daily, handles waiver claims and responds to trades

Everything funnels decisions through the Claude brain and actions through the
write client, which is DRY-RUN unless you've set ENABLE_WRITES=true + a token.
"""

from config import (SLEEPER_LEAGUE_ID, SLEEPER_USERNAME, SLEEPER_SPORT,
                    SLEEPER_SEASON, SLEEPER_TOKEN, ENABLE_WRITES)
from sleeper.read_client import SleeperRead
from sleeper.write_client import SleeperWrite
from brain import claude_manager
from brain.notify import notify


def _ctx():
    read = SleeperRead(SLEEPER_SPORT, SLEEPER_SEASON)
    write = SleeperWrite(SLEEPER_TOKEN, SLEEPER_LEAGUE_ID, ENABLE_WRITES)
    user = read.user(SLEEPER_USERNAME)
    return read, write, user["user_id"]


def _name(all_players, pid):
    p = all_players.get(pid, {})
    return p.get("full_name") or pid


def _player_card(all_players, pid):
    p = all_players.get(pid, {})
    return {
        "player_id": pid,
        "name": p.get("full_name") or pid,
        "pos": p.get("fantasy_positions"),
        "team": p.get("team"),
        "status": p.get("injury_status") or "OK",
    }


# ---------------- LINEUP ----------------
def set_lineup():
    read, write, user_id = _ctx()
    league = read.league(SLEEPER_LEAGUE_ID)
    state = read.state()
    week = state.get("week", 1)
    roster = read.my_roster(SLEEPER_LEAGUE_ID, user_id)
    if not roster:
        notify("Couldn't find your roster — has the draft happened yet?")
        return
    all_players = read.all_players()

    # drop empty/zero slots from the player list — Sleeper sometimes carries "0"
    # as a placeholder for an unfilled roster spot
    player_ids = [pid for pid in (roster.get("players") or []) if pid and str(pid) != "0"]
    candidates = [_player_card(all_players, pid) for pid in player_ids]

    # starting slots only — BN (bench) and IR/RES (reserve/injured reserve) get
    # filled automatically with whoever isn't a starter
    all_positions = league.get("roster_positions", [])
    starting_slots = [p for p in all_positions if p not in ("BN", "IR", "RES", "TAXI")]

    rec = claude_manager.lineup_decision({
        "starting_slots": starting_slots,
        "num_starters_required": len(starting_slots),
        "candidates": candidates,
    })

    if rec.get("_parse_error"):
        notify(f"Lineup: couldn't parse brain output.\n{rec.get('_raw','')[:400]}")
        return

    starters = rec.get("starters", [])
    pretty = ", ".join(_name(all_players, pid) for pid in starters)
    notify(f"🧠 Lineup decision: {pretty}\n   {rec.get('reason','')}")

    # re-read roster right before writing so we don't clobber a manual change
    fresh = read.my_roster(SLEEPER_LEAGUE_ID, user_id)
    if fresh.get("starters") == starters:
        notify("   Lineup already matches — nothing to submit.")
        return
    write.set_lineup(roster_id=roster["roster_id"],
                     starters=starters,
                     leg=week,
                     reserve=rec.get("reserve", []))


# ---------------- WAIVERS + TRADES ----------------
def manage_team():
    read, write, user_id = _ctx()
    state = read.state()
    week = state.get("week", 1)
    roster = read.my_roster(SLEEPER_LEAGUE_ID, user_id)
    if not roster:
        notify("Couldn't find your roster — has the draft happened yet?")
        return
    all_players = read.all_players()

    _handle_waivers(read, write, roster, all_players)
    _handle_trades(read, write, roster, all_players, user_id, week)


def _handle_waivers(read, write, roster, all_players):
    owned = read.rostered_player_ids(SLEEPER_LEAGUE_ID)
    # free agents = relevant players nobody owns
    free_agents = []
    for pid, p in all_players.items():
        if pid in owned or not p.get("fantasy_positions"):
            continue
        rank = p.get("search_rank")
        if rank is None or rank > 1500:
            continue
        free_agents.append((rank, _player_card(all_players, pid)))
    free_agents.sort(key=lambda x: x[0])
    fa_top = [c for _, c in free_agents[:30]]

    trending = read.trending("add", hours=24, limit=15)
    trending_named = [{"name": _name(all_players, t["player_id"]),
                       "player_id": t["player_id"], "count": t["count"]} for t in trending]

    roster_cards = [_player_card(all_players, pid) for pid in (roster.get("players") or [])]
    faab_left = 100 - (roster.get("settings", {}).get("waiver_budget_used", 0) or 0)
    open_slots = max(0, 14 - len(roster.get("players") or []))

    rec = claude_manager.waiver_decision({
        "your_roster": roster_cards,
        "free_agents_top": fa_top,
        "trending_adds": trending_named,
        "faab_left": faab_left,
        "open_slots": open_slots,
    })
    if rec.get("_parse_error"):
        notify(f"Waivers: couldn't parse brain output.\n{rec.get('_raw','')[:400]}")
        return

    claims = rec.get("claims", [])
    if not claims:
        notify(f"🧠 Waivers: no moves this cycle. {rec.get('note','')}")
        return
    for c in claims:
        notify(f"🧠 Waiver claim: ADD {c.get('name')} "
               f"DROP {_name(all_players, c.get('drop')) if c.get('drop') else '(open slot)'} "
               f"for ${c.get('faab')} — {c.get('reason','')}")
        try:
            write.waiver_claim(roster_id=roster["roster_id"],
                               add_player_id=c.get("add"),
                               drop_player_id=c.get("drop"),
                               faab_bid=int(c.get("faab", 0)))
        except RuntimeError as e:
            msg = str(e).lower()
            # If the player isn't on waivers (already a free agent), there's no
            # claim to place — add them directly instead. This is a different
            # mutation, not a retry of the same write, so it's safe.
            if any(k in msg for k in ("waiver", "claim", "passed", "free agent")):
                notify("   ↳ Not on waivers — adding directly as a free agent.")
                try:
                    write.add_free_agent(roster_id=roster["roster_id"],
                                         add_player_id=c.get("add"),
                                         drop_player_id=c.get("drop"))
                except RuntimeError as e2:
                    notify(f"   ✗ Free-agent add also failed: {e2}")
            else:
                notify(f"   ✗ Waiver claim failed: {e}")


def _handle_trades(read, write, roster, all_players, user_id, week):
    txns = read.transactions(SLEEPER_LEAGUE_ID, week)
    rosters = read.rosters(SLEEPER_LEAGUE_ID)
    roster_by_id = {r["roster_id"]: r for r in rosters}

    pending = [t for t in txns
               if t.get("type") == "trade" and t.get("status") == "pending"]
    incoming = [t for t in pending if roster["roster_id"] in (t.get("roster_ids") or [])
                and t.get("creator") != user_id]

    if not incoming:
        return

    for t in incoming:
        # adds/drops describe who goes where: {player_id: roster_id}
        incoming_desc = {
            "transaction_id": t.get("transaction_id"),
            "adds": {_name(all_players, pid): rid for pid, rid in (t.get("adds") or {}).items()},
            "drops": {_name(all_players, pid): rid for pid, rid in (t.get("drops") or {}).items()},
            "draft_picks": t.get("draft_picks"),
        }
        other_ids = [rid for rid in (t.get("roster_ids") or []) if rid != roster["roster_id"]]
        their_roster = roster_by_id.get(other_ids[0], {}) if other_ids else {}

        rec = claude_manager.evaluate_trade({
            "incoming_trade": incoming_desc,
            "my_roster": [_player_card(all_players, pid) for pid in (roster.get("players") or [])],
            "their_roster": [_player_card(all_players, pid) for pid in (their_roster.get("players") or [])],
        })
        if rec.get("_parse_error"):
            notify(f"Trade {t.get('transaction_id')}: couldn't parse brain output.")
            continue

        verdict = "ACCEPT" if rec.get("accept") else "REJECT"
        notify(f"🧠 Trade {t.get('transaction_id')}: {verdict} — {rec.get('reason','')}")
        if rec.get("counter"):
            notify(f"   Suggested counter: {rec['counter']}")
        write.respond_to_trade(
            transaction_id=t.get("transaction_id"),
            leg=t.get("leg", week),
            accept=bool(rec.get("accept")),
        )
