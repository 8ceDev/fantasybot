"""In-season autopilot: lineup, waivers, and trades.

Two entry points, both called from run.py and scheduled on GitHub Actions:

  set_lineup()      -> daily (7am ET), fills this WEEK's unlocked starting slots
  manage_team()     -> daily (6am ET), waiver claims + responds to trades

This league uses WEEKLY lineups: a starting slot earns every game its player
plays that week, and each player locks into his slot (starter or bench) at his
first tip-off of the week. So a player's lineup value is per-game value x games
left this week. The code does that optimization deterministically; the Claude
brain supplies what the numbers can't see (injury news, rest, role changes) and
the trade/waiver judgment calls.

Every write goes through the write client, which is DRY-RUN unless you've set
ENABLE_WRITES=true + a token.
"""

import time

from config import (SLEEPER_LEAGUE_ID, SLEEPER_USERNAME, SLEEPER_SPORT,
                    SLEEPER_SEASON, SLEEPER_TOKEN, ENABLE_WRITES)
from sleeper.read_client import SleeperRead
from sleeper.write_client import SleeperWrite
from brain import claude_manager
from brain.scouting import (Scout, assign_lineup_slots, _slot_allows,
                            fpg_estimate, week_value)
from brain.notify import notify

# How long a trade can sit in "negotiate" before we close it out as stale.
NEGOTIATION_MAX_DAYS = 3

# Injury statuses that mean a player can't help the active roster right now.
# Used both to keep unavailable players out of the lineup/backfill and to decide
# who is IR-eligible (stashable) vs. healthy enough to activate off IR. Sleeper
# is the final arbiter of IR-eligibility — we catch any rejection — but gating on
# these first avoids obviously-doomed attempts (e.g. IR-ing a healthy player).
UNAVAILABLE = {"Out", "IR", "Injured Reserve", "Doubtful", "Suspension",
               "NA", "DNR", "Sidelined", "PUP", "COV", "Sus", "Out For Season"}


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


def _notify_matchup(team_context):
    """Surface the head-to-head the brain is reasoning about, showing BOTH the
    points banked so far and Sleeper's projected final. The two can disagree
    sharply early in the week (lead on banked points, underdog on projection),
    and the projection is what the matchup actually comes down to — so printing
    both makes the bot's reasoning legible instead of hiding a phantom lead."""
    cm = (team_context or {}).get("current_matchup")
    if not cm:
        return
    me = cm.get("my_points_this_week")
    them = cm.get("opponent_points_this_week")
    wk = (team_context or {}).get("week")
    line = f"   📊 Week {wk} vs {cm.get('opponent', 'opponent')}: banked {me}-{them}"
    mp, op = cm.get("my_projected_this_week"), cm.get("opponent_projected_this_week")
    if mp is not None and op is not None:
        fav = "favored" if (mp or 0) >= (op or 0) else "UNDERDOG"
        line += f"  |  projected {mp}-{op} ({fav})"
    notify(line)


def _add_player(write, roster_id, add_id, drop_id, faab):
    """Submit an add (with optional drop), falling back to an instant free-agent
    add when the player isn't on waivers. Returns True on success. Never raises —
    logs the failure and returns False, so one bad add can't abort the cycle.

    The waiver->free_agent fallback is a DIFFERENT mutation, not a retry of the
    same write, so it's safe (see write_client docstring #3)."""
    def _locked_drop(err):
        m = str(err).lower()
        return "locked" in m and "lineup" in m

    try:
        write.waiver_claim(roster_id=roster_id, add_player_id=add_id,
                           drop_player_id=drop_id, faab_bid=int(faab or 0))
        return True
    except RuntimeError as e:
        # The player we're dropping is locked into the current lineup — Sleeper
        # refuses it, and the free-agent fallback would fail identically, so
        # don't bother retrying. Report it as what it is (a lineup problem: the
        # bot is starting a player it wants to drop) rather than the misleading
        # "not on waivers" message the generic fallback would print.
        if _locked_drop(e):
            notify("   ✗ Can't drop that player — they're locked into the current "
                   "starting lineup. Skipping this move until they're benched "
                   "(the lineup shouldn't start a player we want to cut).")
            return False
        msg = str(e).lower()
        if any(k in msg for k in ("waiver", "claim", "passed", "free agent")):
            notify("   ↳ Not on waivers — adding directly as a free agent.")
            try:
                write.add_free_agent(roster_id=roster_id, add_player_id=add_id,
                                     drop_player_id=drop_id)
                return True
            except RuntimeError as e2:
                if _locked_drop(e2):
                    notify("   ✗ Can't drop that player — they're locked into the "
                           "current starting lineup. Skipping until they're benched.")
                else:
                    notify(f"   ✗ Free-agent add also failed: {e2}")
                return False
            except Exception as e2:   # network error: state unknown, never retry
                notify(f"   ✗ Free-agent add errored ({type(e2).__name__}: {e2}) — not retrying.")
                return False
        notify(f"   ✗ Waiver claim failed: {e}")
        return False
    except Exception as e:   # network error: state unknown, never retry
        notify(f"   ✗ Waiver claim errored ({type(e).__name__}: {e}) — not retrying.")
        return False


def _open_active_slots(roster, league):
    """How many active roster slots are open right now (players we can add
    WITHOUT dropping). Mirrors the math in _handle_waivers; IR/reserve players
    sit in separate slots and don't count against the active cap."""
    positions = (league or {}).get("roster_positions", []) or []
    active_cap = len([p for p in positions if p not in ("IR", "RES", "TAXI")]) or 14
    reserve_ids = set(str(p) for p in (roster.get("reserve") or []))
    active = [p for p in (roster.get("players") or []) if str(p) not in reserve_ids]
    return max(0, active_cap - len(active))


def _trade_net_roster_change(trade, roster_id):
    """Net change to MY roster size if this trade processes: players coming TO me
    minus players leaving me. Positive means I need that many open slots (or must
    drop that many more players) for Sleeper to be able to process it."""
    def _mine(d):
        n = 0
        for rid in (d or {}).values():
            try:
                if int(rid) == int(roster_id):
                    n += 1
            except (TypeError, ValueError):
                pass
        return n
    return _mine(trade.get("adds")) - _mine(trade.get("drops"))


def _reconcile_ir(write, roster, league, all_players, want_reserve):
    """Make the roster's IR/reserve slots reflect reality, using the real
    move_to_ir / activate_from_ir mutations (DRY-RUN gated like every write).

      • STASH: players the brain flagged for IR (`want_reserve`) that are
        genuinely injured and not already on IR get moved to IR. That FREES an
        active roster slot — which is the whole point: an out-for-weeks body no
        longer clogs a slot a healthy streamer could score in.
      • ACTIVATE: players sitting on IR who are healthy again are brought back
        onto the active roster when there's an open active slot. We never drop
        anyone to make room — a healthy player with nowhere to land is flagged
        for a human instead of being stranded on IR.

    Every move is wrapped: Sleeper rejects an IR move for an ineligible player
    or an activation with no room, and that rejection is logged, never fatal.
    Returns the set of player_ids moved to IR this run."""
    positions = (league or {}).get("roster_positions", []) or []
    ir_cap = len([p for p in positions if p in ("IR", "RES")])
    active_cap = len([p for p in positions if p not in ("IR", "RES", "TAXI")]) or 14

    def status_of(pid):
        return (all_players.get(str(pid), {}) or {}).get("injury_status") or "OK"

    reserve = [str(p) for p in (roster.get("reserve") or [])]
    reserve_set = set(reserve)
    owned = [str(p) for p in (roster.get("players") or [])]
    moved_to_ir = set()

    if ir_cap == 0:
        return moved_to_ir  # league has no IR slot — nothing to reconcile

    # 1) ACTIVATE healthy players off IR into genuinely open active slots.
    active_now = [p for p in owned if p not in reserve_set]
    open_active = max(0, active_cap - len(active_now))
    for pid in reserve:
        if status_of(pid) in UNAVAILABLE:
            continue  # still hurt — leave on IR
        if open_active > 0:
            notify(f"   🔁 {_name(all_players, pid)} is healthy — activating off IR.")
            try:
                write.activate_from_ir(roster["roster_id"], pid)
                reserve_set.discard(pid)
                open_active -= 1
            except Exception as e:
                notify(f"   ✗ couldn't activate {_name(all_players, pid)}: {e}")
        else:
            notify(f"   ⚠ {_name(all_players, pid)} is healthy but the active roster is "
                   f"full — drop dead weight to bring them back off IR.")

    # 2) STASH injured players the brain wants on IR (each one frees a slot).
    for pid in [str(p) for p in (want_reserve or []) if p and str(p) != "0"]:
        if pid in reserve_set or pid not in owned:
            continue
        if len(reserve_set) >= ir_cap:
            notify(f"   ⚠ IR is full ({ir_cap} slot[s]) — can't stash "
                   f"{_name(all_players, pid)}.")
            break
        if status_of(pid) not in UNAVAILABLE:
            notify(f"   ⚠ not stashing {_name(all_players, pid)} on IR — not listed as "
                   f"injured/out, so Sleeper won't allow it.")
            continue
        notify(f"   🏥 Moving {_name(all_players, pid)} to IR — frees an active slot.")
        try:
            write.move_to_ir(roster["roster_id"], pid)
            reserve_set.add(pid)
            moved_to_ir.add(pid)
        except Exception as e:
            notify(f"   ✗ couldn't move {_name(all_players, pid)} to IR: {e}")
    return moved_to_ir


def _ask_brain(label, fn, context, fallback):
    """Call the Claude brain, but never let an API outage kill a run: on any
    exception, log it and return `fallback` so the deterministic parts (lineup
    optimizer, empty-slot safety net) still do their job."""
    try:
        return fn(context)
    except Exception as e:
        notify(f"   ⚠ {label}: brain call failed ({type(e).__name__}: {e}) — continuing without it.")
        return fallback


# Enriched player cards (with real fantasy-point production) are built by the
# Scout in brain/scouting.py — see scout.cards(). This module only needs plain
# names for its notifications and trade descriptions.


# ---------------- WEEK STATE (shared by lineup, waivers, trades) ----------------
# This league uses WEEKLY lineups: a starting slot earns every game its player
# plays this week, and a player locks into his slot (starter OR bench) at his
# first tip-off of the week, for the rest of the week. Everything below reasons
# in those terms. (The bot used to think in nightly terms — "start whoever plays
# tonight" — which locked scrubs into slots for the whole week while stars who
# played later sat on the bench. That was the single biggest drag on the team.)

def _load_schedule():
    """This week's NBA schedule (games left per team, who has tipped off).
    Best-effort: on failure every caller ranks by per-game value instead."""
    from sleeper.nba_schedule import load_week_schedule
    sched = load_week_schedule()
    notify(f"   🗓 {sched.summary()}")
    return sched


def _current_leg(write, read, roster, week, state, user_id):
    """(committed_starters, target_leg, round_) for our roster this week.

    A matchup week is a `round` holding `legs`; this league has one leg per week
    (leg == week). We still look the leg up rather than assume it, because a
    write aimed at the wrong leg is rejected as illegal."""
    round_ = week
    state_leg = state.get("leg")
    my_legs = [l for l in (write.get_matchup_legs(round_) or [])
               if l.get("roster_id") == roster["roster_id"]]
    target = None
    if state_leg is not None:
        target = next((l for l in my_legs if l.get("leg") == state_leg), None)
    if target is None and my_legs:
        target = max(my_legs, key=lambda l: l.get("leg", 0))
    target_leg = target.get("leg") if target else (state_leg if state_leg is not None else week)
    committed = (target or {}).get("starters")
    if committed is None:
        committed = (read.my_roster(SLEEPER_LEAGUE_ID, user_id) or {}).get("starters") or []
    return [str(p) if p not in (None, "") else "0" for p in committed], target_leg, round_


def _played_this_week(read, week, roster):
    """Players with points on the board this week (they've played -> locked)."""
    mine = (read.my_matchup(SLEEPER_LEAGUE_ID, week, roster["roster_id"]) or {}).get("mine") or {}
    return {str(pid) for pid, p in (mine.get("players_points") or {}).items() if p}


def _lock_ids(player_ids, card_by_id, played, sched, conservative):
    """Who is locked into his current slot for the rest of the week.

      optimistic   -> only players with points on the board (certainly locked)
      conservative -> plus everyone whose NBA team has already tipped off this
                      week (also catches a player who played but scored 0, or
                      sat out injured while his team played)
    The lineup tries optimistic first (most room to improve) and only falls back
    to conservative if Sleeper rejects the write. Drops always use conservative,
    because a rejected drop wastes the whole move."""
    ids = set(played)
    if conservative and sched is not None and getattr(sched, "known", False):
        ids |= {pid for pid in player_ids
                if sched.started_this_week((card_by_id.get(pid) or {}).get("team"))}
    return ids


def _annotate_week(cards, sched, locked_ids, starters):
    """Add the weekly fields the brain and the optimizer reason from."""
    for c in cards:
        pid = c["player_id"]
        gl = sched.games_left(c.get("team")) if sched is not None else None
        c["games_left_this_week"] = gl
        c["value_rest_of_week"] = week_value(c, gl)
        c["locked_this_week"] = pid in locked_ids
        c["in_starting_lineup"] = pid in starters
    return cards


def _week_info(sched, week, open_starting, total_starting):
    info = {
        "week": week,
        "lineup_rules": ("WEEKLY lineups: a starting slot scores every game its "
                         "player plays this week; each player locks into his slot "
                         "(starter or bench) at his first tip-off of the week"),
        "starting_slots_still_open": open_starting,
        "starting_slots_total": total_starting,
    }
    if sched is not None and getattr(sched, "known", False):
        info["days_left_including_today"] = sched.days_left()
        info["week_ends"] = sched.week_end.isoformat()
    else:
        info["schedule"] = ("unknown this run — games_left_this_week is null, so "
                            "compare players on per-game value")
    return info


def _droppable(roster, cards, starters, locked_ids, sched_known, exclude=()):
    """Active (non-IR) players we can actually drop right now, least valuable
    first. Sleeper refuses to drop a player locked into the starting lineup, so
    a current starter only counts if we KNOW he isn't locked (schedule loaded and
    his team hasn't tipped off). IR players are left out: dropping one doesn't
    free an active slot."""
    reserve = {str(p) for p in (roster.get("reserve") or [])}
    excl = {str(p) for p in exclude}

    def ok(c):
        pid = c["player_id"]
        if pid in reserve or pid in excl:
            return False
        if pid in starters:
            return bool(sched_known) and pid not in locked_ids
        return True

    out = [c for c in cards if ok(c)]
    out.sort(key=fpg_estimate)
    return out


def _week_ctx(read, write, scout, roster, week, state, user_id, league, sched):
    """Snapshot of this week's lineup locks, used to keep roster moves legal."""
    positions = (league or {}).get("roster_positions", []) or []
    n = len([p for p in positions if p not in ("BN", "IR", "RES", "TAXI")])
    try:
        committed, _, _ = _current_leg(write, read, roster, week, state, user_id)
    except Exception:
        committed = [str(p) for p in (roster.get("starters") or [])]
    try:
        played = _played_this_week(read, week, roster)
    except Exception:
        played = set()
    player_ids = [str(p) for p in (roster.get("players") or []) if p and str(p) != "0"]
    card_by_id = {c["player_id"]: c for c in scout.cards(player_ids)}
    locked = _lock_ids(player_ids, card_by_id, played, sched, conservative=True)
    padded = (committed + ["0"] * n)[:n]
    open_starting = sum(1 for p in padded if p == "0" or p not in locked)
    return {"sched": sched, "locked": locked,
            "starters": {p for p in committed if p and p != "0"},
            "sched_known": bool(getattr(sched, "known", False)),
            "info": _week_info(sched, week, open_starting, n)}


# ---------------- LINEUP ----------------
def _brain_excludes(rec):
    """{player_id: reason} for players the brain vetoed on news grounds."""
    out = {}
    for x in (rec.get("exclude") or []):
        if isinstance(x, dict) and x.get("player_id"):
            out[str(x["player_id"])] = str(x.get("reason") or "")
        elif isinstance(x, (str, int)) and str(x):
            out[str(x)] = ""
    return out


def _plan_lineup(starting_slots, committed, candidates, locked_ids, reserve_ids, rec):
    """Choose who fills every unlocked starting slot.

    The CODE does the optimization; the brain only adjusts it. Movable players
    are ranked by projected rest-of-week points (per-game value x games left),
    the brain's picks get a 10% nudge so it can break near-ties, anyone it
    vetoed with a news reason is held out, and the ranked list is seated with
    augmenting paths. Greedy-by-value with augmenting paths is exactly optimal
    here: the sets of players that can be seated together form a transversal
    matroid, so this yields the maximum-total-value lineup the open slots allow.
    Players already locked stay pinned to the slot Sleeper has them in."""
    n = len(starting_slots)
    committed = (list(committed) + ["0"] * n)[:n]
    locked = {i: pid for i, pid in enumerate(committed)
              if pid and pid != "0" and pid in locked_ids}
    pos_by_id = {c["player_id"]: c.get("pos") for c in candidates}
    excluded = _brain_excludes(rec)
    prefer = [str(p) for p in (rec.get("starters") or [])]
    prefer_rank = {}
    for i, pid in enumerate(prefer):
        prefer_rank.setdefault(pid, i)

    movable = [c for c in candidates
               if c["player_id"] not in locked_ids and c["player_id"] not in reserve_ids]
    movable_ids = [c["player_id"] for c in movable]
    by_id = {c["player_id"]: c for c in movable}

    def score(c):
        v = c.get("value_rest_of_week") or 0.0
        return v * 1.10 if c["player_id"] in prefer_rank else v

    primary = [c for c in movable
               if c["player_id"] not in excluded
               and c.get("status") not in UNAVAILABLE
               and (c.get("value_rest_of_week") or 0) > 0]
    primary.sort(key=lambda c: (score(c), -prefer_rank.get(c["player_id"], 10 ** 6)),
                 reverse=True)
    ranked = [c["player_id"] for c in primary]
    mode = "projected rest-of-week points"
    if not ranked:
        # No usable numbers at all (stats AND projections missing): fall back to
        # the brain's order, then the roster order — the bot's old behavior.
        mode = "brain order (no stats available)"
        ranked = [p for p in prefer if p in by_id and p not in excluded]
        ranked += [p for p in movable_ids if p not in ranked and p not in excluded
                   and by_id[p].get("status") not in UNAVAILABLE]
    # An empty slot scores nothing, so anyone left over may plug a hole.
    taken = set(ranked)
    fill = sorted((c for c in movable if c["player_id"] not in taken),
                  key=lambda c: c.get("value_rest_of_week") or 0, reverse=True)
    starters, _seated, _benched, holes = assign_lineup_slots(
        starting_slots, ranked, pos_by_id,
        fill_ids=[c["player_id"] for c in fill], locked=locked)
    chosen = set(starters)
    return {
        "starters": starters,
        "locked": locked,
        "excluded": {p: why for p, why in excluded.items() if p in by_id},
        "holes": holes,
        "mode": mode,
        "pos_by_id": pos_by_id,
        "prefer_unseated": [p for p in prefer if p in by_id and p not in chosen
                            and p not in excluded],
    }


def _lineup_is_legal(plan, starting_slots, all_players):
    """Every non-locked seat must be position-legal before we submit (locked
    seats are Sleeper's own and are left exactly as-is)."""
    for i, pid in enumerate(plan["starters"]):
        if pid == "0" or i in plan["locked"]:
            continue
        if not _slot_allows(plan["pos_by_id"].get(pid), starting_slots[i]):
            notify(f"   ✗ pre-submit check failed: {_name(all_players, pid)} ({pid}) "
                   f"pos={plan['pos_by_id'].get(pid)} can't fill slot "
                   f"{starting_slots[i]} — aborting lineup write")
            return False
    return True


def _report_lineup(plan, starting_slots, committed, card_by_id, all_players, rec,
                   retry=False):
    n = len(starting_slots)
    n_locked = len(plan["locked"])
    head = (f"🧠 Lineup{' (retry)' if retry else ''}: {n - n_locked} of {n} starting "
            f"slot(s) open this week" + (f", {n_locked} locked in" if n_locked else "")
            + f" — ranked by {plan['mode']}")
    lines = [head]
    for i, pid in enumerate(plan["starters"]):
        if i in plan["locked"]:
            continue
        slot = starting_slots[i]
        if pid == "0":
            lines.append(f"   ▫ {slot}: (empty — no eligible player)")
            continue
        c = card_by_id.get(pid, {})
        gl = c.get("games_left_this_week")
        gl_s = (f"{gl} game{'' if gl == 1 else 's'} left" if gl is not None
                else "games left unknown")
        was = committed[i] if i < len(committed) else "0"
        change = "" if was == pid else (
            f"  (was {_name(all_players, was)})" if was != "0" else "  (was empty)")
        lines.append(f"   ✅ {slot}: {_name(all_players, pid)} — {gl_s}, "
                     f"~{c.get('value_rest_of_week', 0)} pts{change}")
    if plan["locked"]:
        lines.append("   🔒 locked for the week: " + ", ".join(
            f"{starting_slots[i]} {_name(all_players, p)}"
            for i, p in sorted(plan["locked"].items())))
    for pid, why in plan["excluded"].items():
        lines.append(f"   🚫 kept out: {_name(all_players, pid)}" + (f" — {why}" if why else ""))
    if plan["holes"]:
        lines.append("   ⚠ empty slot(s): " + ", ".join(plan["holes"]))
    if rec.get("reason"):
        lines.append(f"   💬 {rec['reason']}")
    notify("\n".join(lines))


def _enforce_roster_limit(read, write, roster, league, all_players, scout, sched, week):
    """If the roster is OVER the active limit, clear the overage so Sleeper stops
    blocking every move. While you're over, Sleeper refuses ALL writes — lineup,
    waiver, trade — with "You are over the roster limit of N. Please drop some
    players before making any moves." (You can land here when a trade processes
    more players in than out.) This gets you legal again, brain-free so it works
    even with the Claude API down:
      1. IR every injured player there's room for — frees a slot, KEEPS the player.
      2. Drop the lowest-value BENCH players (never a current starter, never one
         locked into this week) until legal.
    If IR + bench drops still aren't enough — i.e. the only bodies left to cut are
    players you're actively starting — it STOPS and flags for a human rather than
    cutting someone you're using. A drop reduces the roster, so Sleeper permits it
    even while you're over; that's exactly what it's asking for.
    Returns True if it changed the roster (so the caller re-fetches)."""
    positions = (league or {}).get("roster_positions", []) or []
    active_cap = len([p for p in positions if p not in ("IR", "RES", "TAXI")]) or 14
    ir_cap = len([p for p in positions if p in ("IR", "RES")])
    reserve = {str(p) for p in (roster.get("reserve") or [])}
    active = [str(p) for p in (roster.get("players") or []) if str(p) not in reserve and str(p) != "0"]
    over = len(active) - active_cap
    if over <= 0:
        return False

    notify(f"   ⚠ Roster is {over} OVER the {active_cap}-man limit — Sleeper blocks "
           f"every move (lineup/waiver/trade) until that's fixed. Clearing it now.")

    def status_of(pid):
        return (all_players.get(str(pid), {}) or {}).get("injury_status") or "OK"

    cards = scout.cards(active)
    val = {c["player_id"]: fpg_estimate(c) for c in cards}
    committed = {str(p) for p in (roster.get("starters") or []) if p and str(p) != "0"}
    try:
        played = _played_this_week(read, week, roster)
    except Exception:
        played = set()
    locked = _lock_ids(active, {c["player_id"]: c for c in cards}, played, sched, conservative=True)
    changed = False

    # 1) IR injured players (lowest value first) — keeps them, frees a slot.
    ir_room = ir_cap - len(reserve)
    for pid in sorted((p for p in active if status_of(p) in UNAVAILABLE), key=lambda p: val.get(p, 0)):
        if over <= 0 or ir_room <= 0:
            break
        try:
            write.move_to_ir(roster["roster_id"], pid)
            notify(f"   🏥 Moved {_name(all_players, pid)} (injured) to IR — keeps him, frees a slot.")
            active.remove(pid); over -= 1; ir_room -= 1; changed = True
        except Exception as e:
            notify(f"   ✗ couldn't IR {_name(all_players, pid)}: {e}")

    # 2) Drop the lowest-value BENCH players (never a starter, never locked).
    drop = getattr(write, "drop_player", None)
    bench = sorted((p for p in active if p not in committed and p not in locked),
                   key=lambda p: val.get(p, 0))
    for pid in bench:
        if over <= 0:
            break
        try:
            drop(roster["roster_id"], pid) if drop else \
                write.add_free_agent(roster_id=roster["roster_id"], drop_player_id=pid)
            notify(f"   ➖ Dropped {_name(all_players, pid)} (lowest bench value, "
                   f"~{val.get(pid, 0)} fpg) to get under the limit.")
            active.remove(pid); over -= 1; changed = True
        except Exception as e:
            notify(f"   ✗ couldn't drop {_name(all_players, pid)}: {e}")

    if over > 0:
        notify(f"   ⚠ Still {over} over after IR + bench drops — the only players left "
               f"to cut are ones you're starting, so I won't cut them automatically. "
               f"Drop {over} in the app and the bot can move again.")
    return changed


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
    scout = Scout(read, league=league)
    sched = _load_schedule()
    # An over-limit roster blocks the lineup write too, so clear it first.
    if _enforce_roster_limit(read, write, roster, league, all_players, scout, sched, week):
        roster = read.my_roster(SLEEPER_LEAGUE_ID, user_id) or roster

    # starting slots only — BN/IR/RES/TAXI are filled by whoever isn't starting
    all_positions = league.get("roster_positions", [])
    starting_slots = [p for p in all_positions if p not in ("BN", "IR", "RES", "TAXI")]
    n = len(starting_slots)

    committed, target_leg, round_ = _current_leg(write, read, roster, week, state, user_id)
    committed = (committed + ["0"] * n)[:n]
    played = _played_this_week(read, week, roster)

    # enriched cards (real fpts/game under our scoring) + this week's numbers
    player_ids = [str(p) for p in (roster.get("players") or []) if p and str(p) != "0"]
    candidates = scout.cards(player_ids)
    card_by_id = {c["player_id"]: c for c in candidates}
    reserve_ids = {str(p) for p in (roster.get("reserve") or [])}
    locked_ids = _lock_ids(player_ids, card_by_id, played, sched, conservative=False)
    _annotate_week(candidates, sched, locked_ids, set(committed))

    locked_now = [i for i, p in enumerate(committed) if p != "0" and p in locked_ids]
    open_slots = [starting_slots[i] for i in range(n) if i not in locked_now]

    tctx = scout.team_context(SLEEPER_LEAGUE_ID, roster, user_id, week, write=write)
    tctx["week_info"] = _week_info(sched, week, len(open_slots), n)
    _notify_matchup(tctx)

    rec = _ask_brain("Lineup", claude_manager.lineup_decision, {
        "starting_slots": starting_slots,
        "open_slots": open_slots,
        "locked": [{"slot": starting_slots[i], "player_id": committed[i],
                    "name": _name(all_players, committed[i])} for i in locked_now],
        "candidates": candidates,
        "team_context": tctx,
    }, fallback={})
    if rec.get("_parse_error"):
        # Never skip the lineup just because the brain's reply was malformed —
        # the optimizer can set a sound lineup from the numbers alone.
        notify("   ⚠ Lineup: couldn't parse the brain's reply — setting the lineup "
               "purely by projected rest-of-week points.")
        rec = {}

    # IR first: stash the injured players the brain flagged (frees an active
    # slot for the next waiver run) and bring healthy ones back.
    moved = _reconcile_ir(write, roster, league, all_players, rec.get("reserve", []))
    reserve_ids |= set(moved or ())

    plan = _plan_lineup(starting_slots, committed, candidates, locked_ids, reserve_ids, rec)
    if not _lineup_is_legal(plan, starting_slots, all_players):
        return
    _report_lineup(plan, starting_slots, committed, card_by_id, all_players, rec)
    if plan["starters"] == committed:
        notify("   Lineup already optimal — nothing to submit.")
        return

    try:
        write.set_lineup(roster_id=roster["roster_id"], starters=plan["starters"],
                         leg=target_leg, round_=round_)
        return
    except RuntimeError as e:
        msg = str(e).lower()
        if not (getattr(sched, "known", False) and any(k in msg for k in ("illegal", "lock"))):
            notify(f"   ✗ Lineup write failed: {e}")
            return
    except Exception as e:
        notify(f"   ✗ Lineup write errored ({type(e).__name__}: {e}).")
        return

    # Sleeper said a player we tried to move is locked. A lineup write is a full
    # replacement (not an add/drop), so resubmitting is safe: pin everyone whose
    # team has already tipped off this week and try once more.
    notify("   ↻ Sleeper rejected that lineup (a player we tried to move is already "
           "locked). Retrying with everyone whose team has tipped off pinned in place.")
    locked2 = _lock_ids(player_ids, card_by_id, played, sched, conservative=True)
    for c in candidates:
        c["locked_this_week"] = c["player_id"] in locked2
    plan2 = _plan_lineup(starting_slots, committed, candidates, locked2, reserve_ids, rec)
    if not _lineup_is_legal(plan2, starting_slots, all_players):
        return
    if plan2["starters"] == committed:
        notify("   Nothing is movable once locks are accounted for — lineup left as-is.")
        return
    _report_lineup(plan2, starting_slots, committed, card_by_id, all_players, rec, retry=True)
    try:
        write.set_lineup(roster_id=roster["roster_id"], starters=plan2["starters"],
                         leg=target_leg, round_=round_)
    except Exception as e2:
        notify(f"   ✗ Lineup write failed again: {e2}")


# ---------------- WAIVERS + TRADES ----------------
def manage_team():
    read, write, user_id = _ctx()
    league = read.league(SLEEPER_LEAGUE_ID)
    state = read.state()
    week = state.get("week", 1)
    roster = read.my_roster(SLEEPER_LEAGUE_ID, user_id)
    if not roster:
        notify("Couldn't find your roster — has the draft happened yet?")
        return
    all_players = read.all_players()
    # one Scout, one schedule, one team-context snapshot shared by waivers +
    # trades. `write` lets team_context include Sleeper's projected final score.
    scout = Scout(read, league=league)
    sched = _load_schedule()
    # An over-limit roster blocks waivers AND trades, so clear it before anything.
    if _enforce_roster_limit(read, write, roster, league, all_players, scout, sched, week):
        roster = read.my_roster(SLEEPER_LEAGUE_ID, user_id) or roster
    week_ctx = _week_ctx(read, write, scout, roster, week, state, user_id, league, sched)
    team_context = scout.team_context(SLEEPER_LEAGUE_ID, roster, user_id, week, write=write)
    team_context["week_info"] = week_ctx["info"]
    _notify_matchup(team_context)

    _handle_waivers(read, write, roster, all_players, scout, team_context, week_ctx=week_ctx)
    _handle_trades(read, write, roster, all_players, user_id, week,
                   scout=scout, team_context=team_context, week_ctx=week_ctx)


def _clean_id(value):
    """Normalize a player id from the brain ('null', '', 0 -> None)."""
    if value in (None, "", 0, "0"):
        return None
    s = str(value).strip()
    return None if s.lower() in ("null", "none") else s


def _handle_waivers(read, write, roster, all_players, scout=None, team_context=None,
                    week_ctx=None):
    scout = scout or Scout(read, league=read.league(SLEEPER_LEAGUE_ID))
    wc = week_ctx or {}
    sched = wc.get("sched")
    starters = wc.get("starters") or {str(p) for p in (roster.get("starters") or [])}
    locked = wc.get("locked") or set()
    sched_known = wc.get("sched_known", False)

    # free agents ranked by REAL fantasy value under our scoring, not name rank
    fa_top = _annotate_week(scout.ranked_free_agents(SLEEPER_LEAGUE_ID, limit=30),
                            sched, set(), set())

    trending = read.trending("add", hours=24, limit=15)
    trending_named = [{"name": _name(all_players, t["player_id"]),
                       "player_id": t["player_id"], "count": t["count"]} for t in trending]

    roster_cards = _annotate_week(scout.cards(roster.get("players") or []),
                                  sched, locked, starters)
    faab_left = 100 - (roster.get("settings", {}).get("waiver_budget_used", 0) or 0)

    # Open ACTIVE roster slots = how many players we can add WITHOUT dropping
    # anyone. IR/reserve players sit in separate slots and don't count.
    league = scout.league if isinstance(scout.league, dict) else read.league(SLEEPER_LEAGUE_ID)
    positions = (league or {}).get("roster_positions", []) or []
    active_cap = len([p for p in positions if p not in ("IR", "RES", "TAXI")]) or 14
    reserve_ids = {str(p) for p in (roster.get("reserve") or [])}
    active_players = [p for p in (roster.get("players") or []) if str(p) not in reserve_ids]
    open_slots = max(0, active_cap - len(active_players))
    notify(f"   roster: {len(active_players)}/{active_cap} active slots filled"
           + (f", {len(reserve_ids)} on IR" if reserve_ids else "")
           + f" → {open_slots} open")

    # Who can legally be dropped right now (a locked starter can't).
    droppable = _droppable(roster, roster_cards, starters, locked, sched_known)
    droppable_ids = {c["player_id"] for c in droppable}
    for c in roster_cards:
        c["droppable_now"] = c["player_id"] in droppable_ids

    rec = _ask_brain("Waivers", claude_manager.waiver_decision, {
        "your_roster": roster_cards,
        "free_agents_top": fa_top,
        "trending_adds": trending_named,
        "faab_left": faab_left,
        "open_slots": open_slots,
        "droppable_player_ids": [c["player_id"] for c in droppable],
        "team_context": team_context,
    }, fallback={"claims": [], "note": "(brain unavailable)"})
    if rec.get("_parse_error"):
        notify(f"Waivers: couldn't parse brain output.\n{rec.get('_raw','')[:400]}")
        rec = {"claims": []}   # still run the empty-slot safety net below

    claims = rec.get("claims") or []
    taken = set()   # players added this run — so the backfill never re-adds one
    if not claims:
        notify(f"🧠 Waivers: no moves this cycle. {rec.get('note','')}")
    for c in claims:
        add_id = _clean_id(c.get("add"))
        drop_id = _clean_id(c.get("drop"))
        name = c.get("name") or _name(all_players, add_id)
        if not add_id:
            continue
        # Pre-validate instead of letting Sleeper bounce the move.
        if drop_id and drop_id not in droppable_ids:
            why = ("he's locked into this week's starting lineup" if drop_id in starters
                   else "he isn't a droppable player on the active roster")
            notify(f"🧠 Waiver claim skipped: ADD {name} DROP {_name(all_players, drop_id)} "
                   f"— can't drop him: {why}.")
            continue
        if not drop_id and open_slots <= 0:
            notify(f"🧠 Waiver claim skipped: ADD {name} with no drop, but the roster is full.")
            continue
        notify(f"🧠 Waiver claim: ADD {name} "
               f"DROP {_name(all_players, drop_id) if drop_id else '(open slot)'} "
               f"for ${c.get('faab')} — {c.get('reason','')}")
        if _add_player(write, roster["roster_id"], add_id, drop_id, c.get("faab", 0)):
            taken.add(add_id)
            if drop_id:
                droppable_ids.discard(drop_id)   # can't drop the same player twice
            else:                                 # a no-drop add consumed a slot
                open_slots = max(0, open_slots - 1)

    # SAFETY NET — never leave an active roster slot empty. An empty slot scores
    # zero all week and quietly drags win odds. We only ADD (never drop) into
    # genuinely empty slots, best available first, skipping anyone unavailable.
    if open_slots > 0:
        owned = {str(p) for p in (roster.get("players") or [])} | taken
        fillable = [c for c in fa_top
                    if str(c.get("player_id")) not in owned
                    and c.get("status") not in UNAVAILABLE]
        # in a weekly league, prefer whoever scores most for the rest of this week
        fillable.sort(key=lambda c: c.get("value_rest_of_week") or 0, reverse=True)
        if fillable:
            notify(f"🧠 {open_slots} empty roster slot(s) — filling with the best "
                   f"available free agents (no drop, $0).")
        for c in fillable:
            if open_slots <= 0:
                break
            add_id = str(c.get("player_id"))
            fpg = c.get("fpts_per_game") or c.get("proj_fpts_per_game") or "?"
            notify(f"   ➕ ADD {c.get('name')} (open slot) — {fpg} fpg")
            if _add_player(write, roster["roster_id"], add_id, None, 0):
                owned.add(add_id)
                open_slots -= 1


# Most players we'll ever drop at once to make room for an accepted trade.
# Anything bigger is too large a roster shake-up to do unattended.
MAX_MAKE_ROOM_DROPS = 2
_TRADE_GONE = ("no longer proposed", "status is now", "cancel", "already",
               "not proposed", "expired", "no longer", "does not exist")


def _same_roster(rid, roster_id):
    try:
        return int(rid) == int(roster_id)
    except (TypeError, ValueError):
        return False


def _trade_status(write, roster_id, tx_id):
    """Current status of a trade, or None if it can't be read."""
    try:
        for tr in write.get_trades(roster_id=roster_id) or []:
            if str(tr.get("transaction_id")) == str(tx_id):
                return tr.get("status")
    except Exception:
        return None
    return None


def _accept_took(write, roster_id, tx_id):
    """After accept_trade returns OK, confirm Sleeper actually moved the trade
    off 'proposed' before we tell anyone it's done. In dry-run there is nothing
    to verify. If the status can't be read we fall back to trusting the OK."""
    if not getattr(write, "enable_writes", False):
        return True
    time.sleep(1.5)
    return _trade_status(write, roster_id, tx_id) != "proposed"


def _clear_room(write, roster_id, drops, all_players):
    """Drop players (drop-only free-agent transactions) to make room for an
    incoming trade. Stops at the first failure and reports what was done."""
    done = []
    drop = getattr(write, "drop_player", None)
    for pid in drops:
        try:
            if drop:
                drop(roster_id, pid)
            else:
                write.add_free_agent(roster_id=roster_id, add_player_id=None, drop_player_id=pid)
            done.append(pid)
            notify(f"    ➖ Dropped {_name(all_players, pid)} to make room.")
        except Exception as e:
            extra = (f" Already dropped {', '.join(_name(all_players, p) for p in done)} — "
                     f"the empty slot gets filled on the next manage run." if done else "")
            notify(f"    ✗ Couldn't drop {_name(all_players, pid)} ({e}) — NOT accepting "
                   f"the trade.{extra}")
            return False
    return True


def _handle_trades(read, write, roster, all_players, user_id, week, preview=False,
                   scout=None, team_context=None, week_ctx=None, fresh_minutes=None):
    """Decide and act on incoming trades: accept / negotiate / reject, each with
    an in-character DM follow-up where a DM is reachable.

    - accept   -> (make roster room if needed) accept_trade, confirm it took,
                  then a curt confirmation DM
    - reject   -> reject_trade + snarky brush-off DM
    - negotiate-> leave the trade pending, DM what would make it work.
                  If it's dragged past NEGOTIATION_MAX_DAYS, close it as stale.

    fresh_minutes: when set (the 20-minute DM job), only trades proposed within
    that window are (re)evaluated by the brain; older open offers are left for
    the daily manage run. Without this, an open offer was re-sent to Claude ~72
    times a day — real money for the same answer.

    Returns (resolved, negotiate_map):
      resolved      -> proposer user_ids we accepted/rejected (banter skips them)
      negotiate_map -> {proposer_user_id: incoming_trade} still open, for the DM
                       negotiation path to carry (and possibly close with a counter).
    """
    scout = scout or Scout(read, league=read.league(SLEEPER_LEAGUE_ID))
    league = scout.league if isinstance(getattr(scout, "league", None), dict) else read.league(SLEEPER_LEAGUE_ID)
    rosters = read.rosters(SLEEPER_LEAGUE_ID)
    roster_by_id = {r["roster_id"]: r for r in rosters}
    name_by_uid = {str(u.get("user_id")): u.get("display_name")
                   for u in read.league_users(SLEEPER_LEAGUE_ID)}
    my_rid = roster["roster_id"]

    wc = week_ctx or {}
    starters_now = wc.get("starters") or {str(p) for p in (roster.get("starters") or [])}
    locked_now = wc.get("locked") or set()
    sched_known = wc.get("sched_known", False)

    handled = set()
    negotiate_map = {}

    # Open trade offers are NOT in the public REST API (completed-only) — they
    # live behind the authenticated GraphQL endpoint, with status "proposed".
    try:
        trades = write.get_trades(roster_id=my_rid)
    except Exception as e:
        notify(f"Trades: couldn't fetch open offers: {e}")
        return handled, negotiate_map

    OPEN = {"proposed", "pending"}
    incoming = [t for t in trades
                if t.get("status") in OPEN
                and my_rid in (t.get("roster_ids") or [])
                and str(t.get("creator")) != str(user_id)]

    if not incoming:
        notify("🧠 Trades: no open offers to act on.")
        return handled, negotiate_map

    my_cards = None   # built lazily — only needed when a trade is evaluated
    now = time.time()
    for t in incoming:
        tx_id = t.get("transaction_id")
        leg = t.get("leg", week)
        other_ids = [rid for rid in (t.get("roster_ids") or []) if rid != my_rid]
        their_roster = roster_by_id.get(other_ids[0], {}) if other_ids else {}
        proposer_uid = str(their_roster.get("owner_id") or "")
        proposer_name = name_by_uid.get(proposer_uid, "a leaguemate")
        created_s = (t.get("created") or 0) / 1000.0
        age_min = (now - created_s) / 60.0 if created_s else 0.0

        if fresh_minutes is not None and age_min > fresh_minutes:
            # Already evaluated on an earlier run; the daily manage run re-checks
            # it. Keep it in the negotiation context so DM replies know about it.
            if proposer_uid:
                negotiate_map[proposer_uid] = t
            notify(f"🧠 Trade {tx_id} from {proposer_name}: still open — re-checked in "
                   f"the daily manage run.")
            continue

        incoming_desc = {
            "transaction_id": tx_id,
            "adds": {_name(all_players, pid): rid for pid, rid in (t.get("adds") or {}).items()},
            "drops": {_name(all_players, pid): rid for pid, rid in (t.get("drops") or {}).items()},
            "draft_picks": t.get("draft_picks"),
        }

        # Roster room: a trade that brings in more players than it sends out can
        # only process if we have the space — so the brain must say who goes.
        if my_cards is None:
            my_cards = scout.cards(roster.get("players") or [])
        need = _trade_net_roster_change(t, my_rid)
        open_slots = _open_active_slots(roster, league)
        must_drop = max(0, need - open_slots)
        outgoing = {str(pid) for pid, rid in (t.get("drops") or {}).items()
                    if _same_roster(rid, my_rid)}
        drop_pool = _droppable(roster, my_cards, starters_now, locked_now, sched_known,
                               exclude=outgoing)[:6] if must_drop else []
        roster_room = {
            "open_roster_slots": open_slots,
            "net_players_if_accepted": need,
            "must_drop_to_accept": must_drop,
            "droppable_least_valuable_first": [
                {"player_id": c["player_id"], "name": c["name"],
                 "fpts_per_game_est": fpg_estimate(c), "status": c.get("status")}
                for c in drop_pool],
        }

        # Reachable DM with the proposer (accepted threads only) for context + replies
        dm = write.find_dm_with(user_id, proposer_uid) if proposer_uid else None
        dm_history = "(none)"
        if dm and dm.get("dm_id"):
            try:
                msgs = write.dm_messages(dm["dm_id"], limit=100)
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

        rec = _ask_brain(f"Trade {tx_id}", claude_manager.evaluate_trade, {
            "incoming_trade": incoming_desc,
            "my_roster": my_cards,
            "their_roster": scout.cards(their_roster.get("players") or []),
            "their_name": proposer_name,
            "dm_history": dm_history,
            "roster_room": roster_room,
            "team_context": team_context,
        }, fallback={"_parse_error": True})
        if rec.get("_parse_error"):
            notify(f"Trade {tx_id}: no usable brain decision — leaving it open.")
            if proposer_uid:
                negotiate_map[proposer_uid] = t
            continue

        decision = (rec.get("decision") or "reject").lower()
        dm_message = rec.get("dm_message", "")

        # Stale negotiation -> close it out
        age_days = age_min / 1440.0
        if decision == "negotiate" and age_days > NEGOTIATION_MAX_DAYS:
            decision = "reject"
            rec["reason"] = (rec.get("reason", "") + " [stale — closing it out]").strip()
            dm_message = dm_message or "This has sat too long with no movement. I'm out."

        notify(f"🧠 Trade {tx_id} from {proposer_name}: {decision.upper()} — {rec.get('reason','')}")

        # NEGOTIATE -> leave the formal offer pending and hand the conversation
        # (and any counter-proposal) to the DM negotiation path.
        if decision == "negotiate":
            if proposer_uid:
                negotiate_map[proposer_uid] = t
            if preview:
                notify("    ↪ negotiate — handed to the DM negotiation path")
            continue

        # Pick (and validate) who we'd drop to make room, before anything is sent.
        drops = []
        if decision == "accept" and must_drop:
            pool = {c["player_id"] for c in drop_pool}
            for d in (rec.get("drop_to_make_room") or []):
                d = _clean_id(d)
                if d and d in pool and d not in drops:
                    drops.append(d)
            drops = drops[:must_drop]
            if must_drop > MAX_MAKE_ROOM_DROPS or len(drops) < must_drop:
                why = (f"it would mean dropping {must_drop} players — too big a shake-up "
                       f"to do unattended" if must_drop > MAX_MAKE_ROOM_DROPS
                       else "the brain didn't name valid players to drop")
                notify(f"    ⚠ Holding trade {tx_id}: it nets +{need} player(s) with "
                       f"{open_slots} open slot(s), so {must_drop} must be dropped first, "
                       f"but {why}. Not accepting, no DM.")
                if proposer_uid:
                    negotiate_map[proposer_uid] = t
                continue

        # ACCEPT / REJECT are terminal — handle them here, once.
        if proposer_uid:
            handled.add(proposer_uid)

        if preview:
            if drops:
                notify(f"    ➖ WOULD drop to make room: "
                       + ", ".join(_name(all_players, d) for d in drops))
            if dm_message:
                reach = "" if dm else "  (no reachable DM — would skip send)"
                notify(f"    💬 WOULD {decision.upper()} + DM: {dm_message}{reach}")
            continue

        if drops:
            # Never drop anyone for a trade that just vanished.
            if _trade_status(write, my_rid, tx_id) not in OPEN:
                notify(f"    ↷ Trade {tx_id} is no longer open — not dropping anyone.")
                continue
            if not _clear_room(write, my_rid, drops, all_players):
                continue

        try:
            write.respond_to_trade(transaction_id=tx_id, leg=leg,
                                   accept=(decision == "accept"))
        except RuntimeError as e:
            # RACE: a leaguemate can withdraw/cancel an offer (or it can expire,
            # or be resolved elsewhere) between our fetch and our write. For a
            # REJECT that's the outcome we wanted; for an ACCEPT the offer is
            # simply gone. Either way it's a no-op — never a reason to abort the
            # whole run (one stale offer used to crash everything after it).
            msg = str(e).lower()
            if "lock" in msg:
                notify(f"    ⚠ Can't {decision} trade {tx_id} right now — it involves a "
                       f"player locked into this week's lineup. It stays open; the next "
                       f"run retries once he unlocks. ({e})")
                if proposer_uid:
                    handled.discard(proposer_uid)
                    negotiate_map[proposer_uid] = t
            elif any(k in msg for k in _TRADE_GONE):
                notify(f"    ↷ Trade {tx_id} already withdrawn/expired — "
                       f"nothing to {decision}, skipping.")
            else:
                notify(f"    ✗ couldn't {decision} trade {tx_id}: {e}")
            continue
        except Exception as e:   # network error: unknown whether it went through
            notify(f"    ✗ {decision} of trade {tx_id} errored ({type(e).__name__}: {e}) — "
                   f"not DMing; the next run will see its real status.")
            continue

        # Only tell the other manager it's done once Sleeper agrees it's done.
        if decision == "accept" and not _accept_took(write, my_rid, tx_id):
            notify(f"    ⚠ accept_trade returned OK but Sleeper still lists trade {tx_id} "
                   f"as proposed — not sending a confirmation DM.")
            continue

        if dm and dm_message:
            try:
                write.send_dm_message(dm["dm_id"], dm_message)
                notify(f"    💬 DM'd {proposer_name}: {dm_message}")
            except Exception as e:
                notify(f"    ✗ couldn't DM {proposer_name}: {e}")

    return handled, negotiate_map
