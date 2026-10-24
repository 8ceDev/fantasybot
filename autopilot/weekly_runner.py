"""In-season autopilot: lineup, waivers, and trades.

Two entry points, both called from run.py and scheduled on Render:

  set_lineup()      -> daily, picks who starts today
  manage_team()     -> daily, handles waiver claims and responds to trades

Everything funnels decisions through the Claude brain and actions through the
write client, which is DRY-RUN unless you've set ENABLE_WRITES=true + a token.
"""

import time

from config import (SLEEPER_LEAGUE_ID, SLEEPER_USERNAME, SLEEPER_SPORT,
                    SLEEPER_SEASON, SLEEPER_TOKEN, ENABLE_WRITES)
from sleeper.read_client import SleeperRead
from sleeper.write_client import SleeperWrite
from brain import claude_manager
from brain.notify import notify

# How long a trade can sit in "negotiate" before we close it out as stale.
NEGOTIATION_MAX_DAYS = 3


def _dm_text(m: dict) -> str:
    """Extract readable text from a DM message's text_map blob."""
    tm = m.get("text_map")
    if isinstance(tm, dict):
        for k in ("en", "text", "default", "value", "body"):
            if tm.get(k):
                return str(tm[k])
        for v in tm.values():
            if isinstance(v, str) and v:
                return v
    if isinstance(tm, str) and tm:
        return tm
    return ""


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


def _handle_trades(read, write, roster, all_players, user_id, week, preview=False):
    """Decide and act on incoming trades: accept / negotiate / reject, each with
    an in-character DM follow-up where a DM is reachable.

    - accept   -> accept_trade + curt confirmation DM
    - reject   -> reject_trade + snarky brush-off DM
    - negotiate-> leave the trade pending, DM what would make it work (only when
                  it's our turn, so we don't nag the same offer every run).
                  If it's dragged past NEGOTIATION_MAX_DAYS, close it as stale.

    Returns the set of proposer user_ids handled, so the banter loop can skip them.
    """
    txns = read.transactions(SLEEPER_LEAGUE_ID, week)
    rosters = read.rosters(SLEEPER_LEAGUE_ID)
    roster_by_id = {r["roster_id"]: r for r in rosters}
    name_by_uid = {str(u.get("user_id")): u.get("display_name")
                   for u in read.league_users(SLEEPER_LEAGUE_ID)}

    pending = [t for t in txns
               if t.get("type") == "trade" and t.get("status") == "pending"]
    incoming = [t for t in pending if roster["roster_id"] in (t.get("roster_ids") or [])
                and t.get("creator") != user_id]

    handled = set()
    if not incoming:
        return handled

    now = time.time()
    for t in incoming:
        tx_id = t.get("transaction_id")
        leg = t.get("leg", week)
        other_ids = [rid for rid in (t.get("roster_ids") or []) if rid != roster["roster_id"]]
        their_roster = roster_by_id.get(other_ids[0], {}) if other_ids else {}
        proposer_uid = str(their_roster.get("owner_id") or "")
        if proposer_uid:
            handled.add(proposer_uid)
        proposer_name = name_by_uid.get(proposer_uid, "a leaguemate")

        incoming_desc = {
            "transaction_id": tx_id,
            "adds": {_name(all_players, pid): rid for pid, rid in (t.get("adds") or {}).items()},
            "drops": {_name(all_players, pid): rid for pid, rid in (t.get("drops") or {}).items()},
            "draft_picks": t.get("draft_picks"),
        }

        # Reachable DM with the proposer (accepted threads only) for context + replies
        dm = write.find_dm_with(user_id, proposer_uid) if proposer_uid else None
        dm_history = "(none)"
        if dm and dm.get("dm_id"):
            try:
                msgs = write.dm_messages(dm["dm_id"], limit=12)
                lines = []
                for m in msgs:
                    who = "me" if str(m.get("author_id")) == str(user_id) else proposer_name
                    txt = _dm_text(m)
                    if txt:
                        lines.append(f"{who}: {txt}")
                if lines:
                    dm_history = "\n".join(lines)
            except Exception:
                pass

        rec = claude_manager.evaluate_trade({
            "incoming_trade": incoming_desc,
            "my_roster": [_player_card(all_players, pid) for pid in (roster.get("players") or [])],
            "their_roster": [_player_card(all_players, pid) for pid in (their_roster.get("players") or [])],
            "their_name": proposer_name,
            "dm_history": dm_history,
        })
        if rec.get("_parse_error"):
            notify(f"Trade {tx_id}: couldn't parse brain output.")
            continue

        decision = (rec.get("decision") or "reject").lower()
        dm_message = rec.get("dm_message", "")

        # Stale negotiation -> close it out
        created_s = (t.get("created") or 0) / 1000.0
        age_days = (now - created_s) / 86400.0 if created_s else 0
        if decision == "negotiate" and age_days > NEGOTIATION_MAX_DAYS:
            decision = "reject"
            rec["reason"] = (rec.get("reason", "") + " [stale — closing it out]").strip()
            dm_message = dm_message or "This has sat too long with no movement. I'm out."

        # During a live negotiation, only message when they spoke last (anti-spam).
        our_turn = (not dm) or (str(dm.get("last_author_id")) != str(user_id))
        send_dm = bool(dm) and bool(dm_message) and (decision in ("accept", "reject") or our_turn)

        notify(f"🧠 Trade {tx_id} from {proposer_name}: {decision.upper()} — {rec.get('reason','')}")

        if preview:
            if dm_message:
                reach = "" if dm else "  (no reachable DM — would skip send)"
                notify(f"    💬 WOULD DM: {dm_message}{reach}")
            continue

        if decision == "accept":
            write.respond_to_trade(transaction_id=tx_id, leg=leg, accept=True)
        elif decision == "reject":
            write.respond_to_trade(transaction_id=tx_id, leg=leg, accept=False)
        # negotiate -> leave the trade pending (no formal action)

        if send_dm:
            try:
                write.send_dm_message(dm["dm_id"], dm_message)
                notify(f"    💬 DM'd {proposer_name}: {dm_message}")
            except Exception as e:
                notify(f"    ✗ couldn't DM {proposer_name}: {e}")

    return handled
