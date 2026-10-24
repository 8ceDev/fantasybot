"""Direct-message autopilot — the chatty negotiator.

Polls your Sleeper DMs, and for any thread where someone else sent the last
(unread) message, fires back an in-character reply from the "iamclaude" GM
persona. Replying also auto-accepts a pending DM invite, so first-time
messagers get answered automatically.

Two entry points:
  handle_dms(preview=True)   -> read + draft replies, SEND NOTHING (safe test)
  handle_dms(preview=False)  -> read + draft + actually send + mark read

Scheduled on GitHub Actions (sleeper-dms.yml). Run `dm-preview` once first to
eyeball the census and the drafted replies before letting it send for real.
"""

from config import (SLEEPER_LEAGUE_ID, SLEEPER_USERNAME, SLEEPER_SPORT,
                    SLEEPER_SEASON, SLEEPER_TOKEN, ENABLE_WRITES,
                    ENABLE_TRADE_PROPOSALS)
from sleeper.read_client import SleeperRead
from sleeper.write_client import SleeperWrite
from autopilot.weekly_runner import _handle_trades
from brain import claude_manager
from brain.notify import notify

OPEN_STATUSES = {"proposed", "pending"}


def _pname(all_players, pid):
    p = all_players.get(pid, {})
    return p.get("full_name") or pid


def _card(all_players, pid):
    p = all_players.get(pid, {})
    return {"player_id": pid, "name": p.get("full_name") or pid,
            "pos": p.get("fantasy_positions"), "team": p.get("team"),
            "status": p.get("injury_status") or "OK"}


def _extract_text(msg: dict) -> str:
    """Pull readable text out of a message. We select text_map (a JSON blob)
    rather than the LocalizedString `text` field, so handle a few shapes."""
    tm = msg.get("text_map")
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


def handle_dms(preview: bool = False):
    if not SLEEPER_TOKEN:
        notify("DM bot: no SLEEPER_TOKEN set — can't read or send DMs.")
        return

    read = SleeperRead(SLEEPER_SPORT, SLEEPER_SEASON)
    write = SleeperWrite(SLEEPER_TOKEN, SLEEPER_LEAGUE_ID, ENABLE_WRITES)

    me = read.user(SLEEPER_USERNAME)
    if not me:
        notify(f"DM bot: couldn't resolve user '{SLEEPER_USERNAME}'.")
        return
    my_user_id = str(me["user_id"])

    try:
        dms = write.my_dms()
    except Exception as e:
        notify(f"DM bot: couldn't list DMs: {e}")
        return

    mode = "PREVIEW (nothing will send)" if preview else "LIVE"
    notify(f"📬 DM run [{mode}] — {len(dms)} thread(s) total.")

    # Context: my roster + a map of league user_id -> their roster (for trade talk)
    all_players = read.all_players()
    rosters = read.rosters(SLEEPER_LEAGUE_ID)
    roster_by_owner = {str(r.get("owner_id")): r for r in rosters}
    my_roster = roster_by_owner.get(my_user_id, {})
    my_roster_names = [_pname(all_players, pid) for pid in (my_roster.get("players") or [])]
    my_roster_cards = [_card(all_players, pid) for pid in (my_roster.get("players") or [])]
    my_rid = my_roster.get("roster_id")

    # 1) Formal trades — accept / reject here; negotiate cases are handed back
    #    for the conversation (and possible counter-proposal) below.
    week = (read.state() or {}).get("week", 1)
    try:
        resolved, negotiate_map = _handle_trades(read, write, my_roster, all_players,
                                                  my_user_id, week, preview=preview)
    except Exception as e:
        notify(f"DM bot: trade pass errored: {e}")
        resolved, negotiate_map = set(), {}

    # Rosters I already have an OUTSTANDING proposal out to (so we don't re-propose).
    outbound_rids = set()
    try:
        for t in write.get_trades(roster_id=my_rid):
            if t.get("status") in OPEN_STATUSES and str(t.get("creator")) == my_user_id:
                for rid in (t.get("roster_ids") or []):
                    if rid != my_rid:
                        outbound_rids.add(rid)
    except Exception:
        pass

    # 2) Banter — reply to everyone else who messaged last.
    replied = 0
    for dm in dms:
        dm_id = str(dm.get("dm_id"))
        last_author = str(dm.get("last_author_id") or "")
        last_text = dm.get("last_message_text") or ""
        last_msg_id = str(dm.get("last_message_id") or "")
        last_read_id = str(dm.get("last_read_id") or "")
        unread = bool(last_msg_id) and last_msg_id != last_read_id

        # Who's on the other side, and is my membership still pending?
        try:
            members = write.dm_members(dm_id)
        except Exception:
            members = []
        others = [m for m in members if str(m.get("user_id")) != my_user_id]
        me_member = next((m for m in members if str(m.get("user_id")) == my_user_id), None)
        pending = me_member.get("pending") if me_member else None
        other_name = (others[0].get("display_name") if others
                      else dm.get("last_author_display_name") or "someone")
        other_id = str(others[0]["user_id"]) if others else None

        # Skip threads already resolved by the trade pass (accepted/rejected).
        if other_id and other_id in resolved:
            notify(f"  • {other_name}: trade accepted/rejected by trade pass.")
            continue

        who_last = "me" if last_author == my_user_id else other_name
        notify(f"  • {other_name}: last_from={who_last}, unread={unread}, "
               f"my_pending={pending}, preview=\"{last_text[:50]}\"")

        # Reply only when THEY sent something I haven't answered.
        if not unread or not last_author or last_author == my_user_id:
            continue

        # Their roster (if they're a league member) for trade context
        their_roster = roster_by_owner.get(other_id, {})
        their_names = [_pname(all_players, pid) for pid in (their_roster.get("players") or [])]
        their_cards = [_card(all_players, pid) for pid in (their_roster.get("players") or [])]
        is_member = bool(their_roster)

        # Conversation history (best-effort; fall back to the last message)
        convo, parent_type = "", "dm"
        try:
            msgs = write.dm_messages(dm_id, limit=15)
            lines = []
            for m in msgs:
                if m.get("parent_type"):
                    parent_type = m["parent_type"]
                txt = _extract_text(m)
                if not txt:
                    continue
                who = "me" if str(m.get("author_id")) == my_user_id else other_name
                lines.append(f"{who}: {txt}")
            convo = "\n".join(lines)
        except Exception as e:
            notify(f"    (couldn't read history: {e})")
        if not convo:
            convo = f"{other_name}: {last_text}"

        # Non-league-members just get banter. League members go through the
        # negotiator, which can also CLOSE a deal by proposing a trade.
        if not is_member:
            reply = claude_manager.scout_reply({
                "my_roster": my_roster_names, "their_name": other_name,
                "their_roster": their_names, "conversation": convo, "latest": last_text,
            })
            if preview:
                notify(f"    💬 WOULD reply: {reply}")
                continue
            try:
                write.send_dm_message(dm_id, reply, parent_type=parent_type)
                replied += 1
                notify(f"    💬 Replied: {reply}")
            except Exception as e:
                notify(f"    ✗ Couldn't send reply: {e}")
            continue

        rec = claude_manager.negotiate({
            "my_roster": my_roster_cards,
            "their_roster": their_cards,
            "their_name": other_name,
            "dm_history": convo,
            "incoming_trade": negotiate_map.get(other_id),
        })
        reply = rec.get("message", "")
        wants_propose = bool(rec.get("propose"))

        # Validate any proposal against the actual rosters before trusting it.
        send_ids = [p for p in (rec.get("send_player_ids") or [])
                    if p in (my_roster.get("players") or [])]
        recv_ids = [p for p in (rec.get("receive_player_ids") or [])
                    if p in (their_roster.get("players") or [])]
        their_rid = their_roster.get("roster_id")
        valid_deal = (wants_propose and send_ids and recv_ids
                      and len(send_ids) == len(rec.get("send_player_ids") or [])
                      and len(recv_ids) == len(rec.get("receive_player_ids") or []))

        send_str = ", ".join(_pname(all_players, p) for p in send_ids)
        recv_str = ", ".join(_pname(all_players, p) for p in recv_ids)

        if wants_propose and not valid_deal:
            notify(f"    ⚠ {other_name}: proposal referenced players not on the "
                   f"right rosters — ignoring the proposal, just replying.")

        already_out = their_rid in outbound_rids

        if preview:
            notify(f"    💬 WOULD reply: {reply}")
            if valid_deal:
                blocked = (" [BLOCKED: proposals disabled]" if not ENABLE_TRADE_PROPOSALS
                           else " [BLOCKED: already have an offer out]" if already_out else "")
                notify(f"    🤝 WOULD PROPOSE: give [{send_str}] for [{recv_str}]{blocked}")
                if rec.get("reject_incoming") and negotiate_map.get(other_id):
                    notify("       (and reject their now-superseded open offer)")
            continue

        # Fire the proposal if allowed, then message.
        proposed = False
        if valid_deal and ENABLE_TRADE_PROPOSALS and not already_out:
            try:
                adds = [(p, their_rid) for p in send_ids] + [(p, my_rid) for p in recv_ids]
                drops = [(p, my_rid) for p in send_ids] + [(p, their_rid) for p in recv_ids]
                write.propose_trade(adds=adds, drops=drops)
                outbound_rids.add(their_rid)
                proposed = True
                notify(f"    🤝 PROPOSED to {other_name}: give [{send_str}] for [{recv_str}]")
                # Supersede their stale open offer if the model said so.
                inc = negotiate_map.get(other_id)
                if rec.get("reject_incoming") and inc:
                    write.respond_to_trade(transaction_id=inc.get("transaction_id"),
                                           leg=inc.get("leg", week), accept=False)
                    notify("       rejected their superseded open offer.")
            except Exception as e:
                notify(f"    ✗ couldn't propose to {other_name}: {e}")
        elif valid_deal and not ENABLE_TRADE_PROPOSALS:
            notify(f"    🤝 (agreement reached, but proposals are OFF — set "
                   f"ENABLE_TRADE_PROPOSALS=true to let it send: [{send_str}] for [{recv_str}])")

        if reply:
            try:
                write.send_dm_message(dm_id, reply, parent_type=parent_type)
                replied += 1
                notify(f"    💬 Replied: {reply}" + ("  [+ proposed trade]" if proposed else ""))
            except Exception as e:
                notify(f"    ✗ Couldn't send reply: {e}")

    if not preview and replied:
        try:
            write.clear_unread_dms()
        except Exception:
            pass  # non-critical; we gate on last_read_id anyway

    notify(f"📬 DM run done — {'previewed only' if preview else f'replied to {replied}'}.")
