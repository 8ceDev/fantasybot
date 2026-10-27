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
from brain.scouting import Scout, assign_lineup_slots
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
    """Surface the live weekly head-to-head the brain is reasoning about, so a
    wrong or stale score is obvious in the run log instead of hiding inside the
    brain's prose. This prints the CUMULATIVE weekly total (summed across every
    scoring day), which is what team_context now feeds the brain — a single-day
    snapshot used to leak through here and read as a phantom lead."""
    cm = (team_context or {}).get("current_matchup")
    if not cm:
        return
    me = cm.get("my_points_this_week")
    them = cm.get("opponent_points_this_week")
    verb = "up" if (me or 0) >= (them or 0) else "down"
    notify(f"   📊 Week so far vs {cm.get('opponent', 'opponent')}: "
           f"{verb} {me}-{them} (total across every day this week)")


def _add_player(write, roster_id, add_id, drop_id, faab):
    """Submit an add (with optional drop), falling back to an instant free-agent
    add when the player isn't on waivers. Returns True on success. Never raises —
    logs the failure and returns False, so one bad add can't abort the cycle.

    The waiver->free_agent fallback is a DIFFERENT mutation, not a retry of the
    same write, so it's safe (see write_client docstring #3)."""
    try:
        write.waiver_claim(roster_id=roster_id, add_player_id=add_id,
                           drop_player_id=drop_id, faab_bid=int(faab or 0))
        return True
    except RuntimeError as e:
        msg = str(e).lower()
        if any(k in msg for k in ("waiver", "claim", "passed", "free agent")):
            notify("   ↳ Not on waivers — adding directly as a free agent.")
            try:
                write.add_free_agent(roster_id=roster_id, add_player_id=add_id,
                                     drop_player_id=drop_id)
                return True
            except RuntimeError as e2:
                notify(f"   ✗ Free-agent add also failed: {e2}")
                return False
        notify(f"   ✗ Waiver claim failed: {e}")
        return False


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
            except RuntimeError as e:
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
        except RuntimeError as e:
            notify(f"   ✗ couldn't move {_name(all_players, pid)} to IR: {e}")
    return moved_to_ir


# Enriched player cards (with real fantasy-point production) are built by the
# Scout in brain/scouting.py — see scout.cards(). This module only needs plain
# names for its notifications and trade descriptions.


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
    scout = Scout(read, league=league)

    # enriched cards (real fpts/game under our scoring) instead of bare names;
    # drop empty/zero slots Sleeper carries for unfilled roster spots
    player_ids = [pid for pid in (roster.get("players") or []) if pid and str(pid) != "0"]
    candidates = scout.cards(player_ids)

    # starting slots only — BN (bench) and IR/RES (reserve/injured reserve) get
    # filled automatically with whoever isn't a starter
    all_positions = league.get("roster_positions", [])
    starting_slots = [p for p in all_positions if p not in ("BN", "IR", "RES", "TAXI")]

    # `write` lets team_context sum the week's legs into the real cumulative
    # score (what the app shows) instead of a single day's snapshot.
    tctx = scout.team_context(SLEEPER_LEAGUE_ID, roster, user_id, week, write=write)
    _notify_matchup(tctx)
    rec = claude_manager.lineup_decision({
        "starting_slots": starting_slots,
        "num_starters_required": len(starting_slots),
        "candidates": candidates,
        "team_context": tctx,
    })

    if rec.get("_parse_error"):
        notify(f"Lineup: couldn't parse brain output.\n{rec.get('_raw','')[:400]}")
        return

    chosen = [str(p) for p in rec.get("starters", [])]
    pos_by_id = {c["player_id"]: c.get("pos") for c in candidates}
    chosen_set = set(chosen)
    fill_ids = [c["player_id"] for c in candidates          # candidates are best-first
                if c["player_id"] not in chosen_set and c.get("status") not in UNAVAILABLE]

    # Make the IR/reserve slots reflect reality BEFORE we submit the lineup:
    # bring any now-healthy player back off IR (into an open active slot) and
    # stash the injured players the brain flagged for reserve. A stash frees an
    # active slot the next waiver run fills with a healthy streamer — the real
    # fix for an out-for-weeks body (e.g. Claxton) clogging the roster.
    _reconcile_ir(write, roster, league, all_players, rec.get("reserve", []))

    # Find the leg we're editing. A matchup week (the `round`) holds the legs;
    # for this league there's one leg per week (weekly lineups). state.leg is
    # the current scoring period; use it to pick our roster's leg.
    round_ = week
    state_leg = state.get("leg")
    my_legs = [l for l in write.get_matchup_legs(round_)
               if l.get("roster_id") == roster["roster_id"]]
    target = None
    if state_leg is not None:
        target = next((l for l in my_legs if l.get("leg") == state_leg), None)
    if target is None and my_legs:
        target = max(my_legs, key=lambda l: l.get("leg", 0))
    target_leg = target.get("leg") if target else (state_leg if state_leg is not None else week)

    # LOCKED PLAYERS. In a weekly lineup a player locks into their slot the
    # moment their game tips off, and Sleeper rejects any write that MOVES a
    # locked player — which is why a full nightly rewrite always failed. So:
    #   • the committed lineup for this leg is our starting point
    #   • any current starter who has already scored this week is LOCKED and
    #     pinned to their exact slot
    #   • only players who haven't played yet are eligible to fill the rest
    # At the very start of a week nothing has scored, so nothing is locked and
    # we set the whole optimal lineup freely.
    committed = (target or {}).get("starters")
    if committed is None:
        committed = read.my_roster(SLEEPER_LEAGUE_ID, user_id).get("starters") or []
    mine = (read.my_matchup(SLEEPER_LEAGUE_ID, week, roster["roster_id"]) or {}).get("mine") or {}
    points = mine.get("players_points") or {}
    played = {str(pid) for pid, p in points.items() if p}   # scored => has played => locked

    locked = {i: str(pid) for i, pid in enumerate(committed)
              if pid and str(pid) != "0" and str(pid) in played}
    # Can't move a player who's already played, so they can't be newly started.
    chosen_free = [p for p in chosen if p not in played]
    fill_free = [p for p in fill_ids if p not in played]

    starters, seated, benched, holes = assign_lineup_slots(
        starting_slots, chosen_free, pos_by_id, fill_ids=fill_free, locked=locked)

    # Safety: verify every NON-locked seat is position-legal before we submit
    # (locked seats are Sleeper's own and are left exactly as-is).
    from brain.scouting import _slot_allows
    for i, pid in enumerate(starters):
        if pid == "0" or i in locked:
            continue
        if not _slot_allows(pos_by_id.get(pid), starting_slots[i]):
            notify(f"   ✗ pre-submit check failed: {_name(all_players, pid)} ({pid}) "
                   f"pos={pos_by_id.get(pid)} can't fill slot {starting_slots[i]} — "
                   f"aborting lineup write")
            return

    pretty = ", ".join(_name(all_players, pid) for pid in starters if pid != "0")
    notify(f"🧠 Lineup decision: {pretty}\n   {rec.get('reason','')}")
    if locked:
        notify(f"   🔒 {len(locked)} slot(s) locked (already played this week) — kept as-is: "
               + ", ".join(_name(all_players, locked[i]) for i in sorted(locked)))
    if benched:
        notify("   ⚠ couldn't legally seat (no open slot, or already played): "
               + ", ".join(_name(all_players, p) for p in benched))
    if holes:
        notify("   ⚠ empty slot(s) with no eligible available player: " + ", ".join(holes))

    if committed == starters:
        notify("   Lineup already matches — nothing to submit.")
        return

    write.set_lineup(roster_id=roster["roster_id"],
                     starters=starters,
                     leg=target_leg,
                     round_=round_)


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
    # one Scout (and one team-context snapshot) shared by waivers + trades.
    # `write` lets team_context report the cumulative weekly score (all days),
    # not a single day's REST snapshot — the difference between the brain
    # thinking it's winning and knowing it has to chase a deficit.
    scout = Scout(read, league=league)
    team_context = scout.team_context(SLEEPER_LEAGUE_ID, roster, user_id, week, write=write)
    _notify_matchup(team_context)

    _handle_waivers(read, write, roster, all_players, scout, team_context)
    _handle_trades(read, write, roster, all_players, user_id, week,
                   scout=scout, team_context=team_context)


def _handle_waivers(read, write, roster, all_players, scout=None, team_context=None):
    scout = scout or Scout(read, league=read.league(SLEEPER_LEAGUE_ID))
    # free agents ranked by REAL fantasy value under our scoring, not name rank
    fa_top = scout.ranked_free_agents(SLEEPER_LEAGUE_ID, limit=30)

    trending = read.trending("add", hours=24, limit=15)
    trending_named = [{"name": _name(all_players, t["player_id"]),
                       "player_id": t["player_id"], "count": t["count"]} for t in trending]

    roster_cards = scout.cards(roster.get("players") or [])
    faab_left = 100 - (roster.get("settings", {}).get("waiver_budget_used", 0) or 0)

    # Open ACTIVE roster slots = how many players we can add WITHOUT dropping
    # anyone. Derive the cap from the league's real roster layout (this league is
    # NOT 14-deep) instead of a hardcoded guess, and don't count players stashed
    # on IR/reserve against the active cap — they sit in separate slots. An empty
    # active slot scores zero every single day, so these must never be left open.
    league = scout.league if isinstance(scout.league, dict) else read.league(SLEEPER_LEAGUE_ID)
    positions = (league or {}).get("roster_positions", []) or []
    active_cap = len([p for p in positions if p not in ("IR", "RES", "TAXI")]) or 14
    reserve_ids = set(str(p) for p in (roster.get("reserve") or []))
    active_players = [p for p in (roster.get("players") or []) if str(p) not in reserve_ids]
    open_slots = max(0, active_cap - len(active_players))
    notify(f"   roster: {len(active_players)}/{active_cap} active slots filled"
           + (f", {len(reserve_ids)} on IR" if reserve_ids else "")
           + f" → {open_slots} open")

    rec = claude_manager.waiver_decision({
        "your_roster": roster_cards,
        "free_agents_top": fa_top,
        "trending_adds": trending_named,
        "faab_left": faab_left,
        "open_slots": open_slots,
        "team_context": team_context,
    })
    if rec.get("_parse_error"):
        notify(f"Waivers: couldn't parse brain output.\n{rec.get('_raw','')[:400]}")
        return

    claims = rec.get("claims", [])
    taken = set()   # players added this run — so the backfill never re-adds one
    if not claims:
        notify(f"🧠 Waivers: no moves this cycle. {rec.get('note','')}")
    for c in claims:
        add_id = c.get("add")
        notify(f"🧠 Waiver claim: ADD {c.get('name')} "
               f"DROP {_name(all_players, c.get('drop')) if c.get('drop') else '(open slot)'} "
               f"for ${c.get('faab')} — {c.get('reason','')}")
        if _add_player(write, roster["roster_id"], add_id, c.get("drop"), c.get("faab", 0)):
            if add_id:
                taken.add(str(add_id))
            if not c.get("drop"):        # a no-drop add consumed one open slot
                open_slots = max(0, open_slots - 1)

    # SAFETY NET — never leave an active roster slot empty. An empty slot scores
    # zero every day and quietly drags win odds, which is exactly what happened
    # here. The brain may or may not choose to fill them; this guarantees it.
    # We only ADD (never drop) into genuinely empty slots, using the best
    # available free agents by our scoring, skipping anyone currently unavailable.
    if open_slots > 0:
        owned = set(str(p) for p in (roster.get("players") or [])) | taken
        fillable = [c for c in fa_top
                    if str(c.get("player_id")) not in owned
                    and c.get("status") not in UNAVAILABLE]
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


def _handle_trades(read, write, roster, all_players, user_id, week, preview=False,
                   scout=None, team_context=None):
    """Decide and act on incoming trades: accept / negotiate / reject, each with
    an in-character DM follow-up where a DM is reachable.

    - accept   -> accept_trade + curt confirmation DM
    - reject   -> reject_trade + snarky brush-off DM
    - negotiate-> leave the trade pending, DM what would make it work (only when
                  it's our turn, so we don't nag the same offer every run).
                  If it's dragged past NEGOTIATION_MAX_DAYS, close it as stale.

    Returns (resolved, negotiate_map):
      resolved      -> proposer user_ids we accepted/rejected (banter skips them)
      negotiate_map -> {proposer_user_id: incoming_trade} left open for the DM
                       negotiation path to carry (and possibly close with a counter).
    """
    scout = scout or Scout(read, league=read.league(SLEEPER_LEAGUE_ID))
    rosters = read.rosters(SLEEPER_LEAGUE_ID)
    roster_by_id = {r["roster_id"]: r for r in rosters}
    name_by_uid = {str(u.get("user_id")): u.get("display_name")
                   for u in read.league_users(SLEEPER_LEAGUE_ID)}

    handled = set()
    negotiate_map = {}

    # Open trade offers are NOT in the public REST API (completed-only) — they
    # live behind the authenticated GraphQL endpoint, with status "proposed".
    try:
        trades = write.get_trades(roster_id=roster["roster_id"])
    except Exception as e:
        notify(f"Trades: couldn't fetch open offers: {e}")
        return handled, negotiate_map

    OPEN = {"proposed", "pending"}
    incoming = [t for t in trades
                if t.get("status") in OPEN
                and roster["roster_id"] in (t.get("roster_ids") or [])
                and str(t.get("creator")) != str(user_id)]

    if not incoming:
        notify("🧠 Trades: no open offers to act on.")
        return handled, negotiate_map

    now = time.time()
    for t in incoming:
        tx_id = t.get("transaction_id")
        leg = t.get("leg", week)
        other_ids = [rid for rid in (t.get("roster_ids") or []) if rid != roster["roster_id"]]
        their_roster = roster_by_id.get(other_ids[0], {}) if other_ids else {}
        proposer_uid = str(their_roster.get("owner_id") or "")
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

        rec = claude_manager.evaluate_trade({
            "incoming_trade": incoming_desc,
            "my_roster": scout.cards(roster.get("players") or []),
            "their_roster": scout.cards(their_roster.get("players") or []),
            "their_name": proposer_name,
            "dm_history": dm_history,
            "team_context": team_context,
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

        notify(f"🧠 Trade {tx_id} from {proposer_name}: {decision.upper()} — {rec.get('reason','')}")

        # NEGOTIATE -> leave the formal offer pending and hand the conversation
        # (and any counter-proposal) to the DM negotiation path.
        if decision == "negotiate":
            if proposer_uid:
                negotiate_map[proposer_uid] = t
            if preview:
                notify("    ↪ negotiate — handed to the DM negotiation path")
            continue

        # ACCEPT / REJECT are terminal — handle them here, once.
        if proposer_uid:
            handled.add(proposer_uid)

        if preview:
            if dm_message:
                reach = "" if dm else "  (no reachable DM — would skip send)"
                notify(f"    💬 WOULD {decision.upper()} + DM: {dm_message}{reach}")
            continue

        if decision == "accept":
            write.respond_to_trade(transaction_id=tx_id, leg=leg, accept=True)
        elif decision == "reject":
            write.respond_to_trade(transaction_id=tx_id, leg=leg, accept=False)

        if dm and dm_message:
            try:
                write.send_dm_message(dm["dm_id"], dm_message)
                notify(f"    💬 DM'd {proposer_name}: {dm_message}")
            except Exception as e:
                notify(f"    ✗ couldn't DM {proposer_name}: {e}")

    return handled, negotiate_map

    return handled
