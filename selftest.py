"""Live, READ-ONLY end-to-end check. Run this ON GitHub Actions (where Sleeper is
reachable) to confirm the bot can SEE everything it needs before you trust it to
act. It never writes, never messages, and never calls Claude — it just pulls the
real data and prints what the brain would be handed.

    python selftest.py

What it verifies against your real league:
  1. Season stats + projections actually load, and their stat keys line up with
     your league's scoring keys (this is the one thing that can't be tested
     offline — if Sleeper renames a key, you'll see it here).
  2. Your roster renders as enriched cards with sane fantasy-points-per-game.
  3. Team context assembles: record, rank, this week's matchup, standings.
  4. Pending trades and DM threads are visible (counts only; no content sent).
"""

import json

import config
from sleeper.read_client import SleeperRead
from sleeper.write_client import SleeperWrite
from brain.scouting import Scout, get_league_scoring


def line(c="-"):
    print(c * 68)


def main():
    config.validate(require_claude=False)
    read = SleeperRead(config.SLEEPER_SPORT, config.SLEEPER_SEASON)

    line("=")
    print("SELFTEST — live read-only check (no writes, no Claude)")
    line("=")

    user = read.user(config.SLEEPER_USERNAME)
    if not user:
        raise SystemExit(f"User '{config.SLEEPER_USERNAME}' not found.")
    user_id = user["user_id"]
    league = read.league(config.SLEEPER_LEAGUE_ID)
    state = read.state()
    week = state.get("week", 1)
    roster = read.my_roster(config.SLEEPER_LEAGUE_ID, user_id)
    print(f"User: {user['display_name']} ({user_id})")
    print(f"League: {league.get('name')} — {league.get('total_rosters')} teams — week {week}")
    if not roster:
        raise SystemExit("No roster found (pre-draft?). Nothing more to check.")

    # ---- 1. stats + projections load, and keys match our scoring ----
    line()
    print("[1] STATS & PROJECTIONS")
    stats = read.season_stats()
    proj = read.season_projections()
    print(f"  season_stats: {len(stats)} players   season_projections: {len(proj)} players")
    weights = get_league_scoring(league)
    print(f"  scoring source: {'league live scoring_settings' if league.get('scoring_settings') else 'hardcoded SCORING_WEIGHTS'}")
    print(f"  scoring keys: {sorted(weights.keys())}")
    # sample one of my players' raw statline to confirm key names
    sample_pid = next((p for p in (roster.get("players") or []) if p in stats), None)
    if sample_pid:
        sline = stats[sample_pid]
        skeys = set(sline.keys())
        matched = [k for k in weights if k in skeys]
        missing = [k for k in weights if k not in skeys]
        print(f"  sample statline keys for {sample_pid}: {sorted(skeys)}")
        print(f"  scoring keys PRESENT in statline: {matched}")
        print(f"  scoring keys MISSING (scored as 0): {missing}")
        if not matched:
            print("  ⚠️  NO scoring keys matched the statline — the stat field names")
            print("      differ from our scoring keys. Paste this output and we'll remap.")
    else:
        print("  ⚠️  none of your rostered players were found in season_stats")
        print("      (early season with no games yet is normal — projections cover it).")

    # ---- 2. enriched roster cards ----
    line()
    print("[2] YOUR ROSTER AS THE BRAIN SEES IT (sorted by fpts/game)")
    scout = Scout(read, league=league, season_stats=stats, projections=proj, weights=weights)
    cards = scout.cards(roster.get("players") or [])
    for c in cards:
        fpg = c.get("fpts_per_game")
        proj_fpg = c.get("proj_fpts_per_game")
        pg = c.get("per_game", "")
        inj = "" if c.get("status") == "OK" else f"  [{c.get('status')}]"
        print(f"  {c['name'][:22]:22} {str(c.get('pos')):10} "
              f"fpg={fpg if fpg is not None else '—':<6} proj={proj_fpg if proj_fpg is not None else '—':<6} "
              f"gp={c.get('games_played','—')}{inj}")
        if pg:
            print(f"      {pg}")
    with_stats = sum(1 for c in cards if c.get("fpts_per_game") is not None)
    print(f"  -> {with_stats}/{len(cards)} players have real stat lines; "
          f"{sum(1 for c in cards if c.get('proj_fpts_per_game') is not None)} have projections.")

    # ---- 3. team context ----
    line()
    print("[3] TEAM CONTEXT")
    reader = (SleeperWrite(config.SLEEPER_TOKEN, config.SLEEPER_LEAGUE_ID, enable_writes=False)
              if config.SLEEPER_TOKEN else None)   # read-only use: projected score
    tc = scout.team_context(config.SLEEPER_LEAGUE_ID, roster, user_id, week, write=reader)
    print(f"  record: {tc.get('my_record')}   rank: {tc.get('my_rank')}")
    cm = tc.get("current_matchup")
    if cm:
        print(f"  this week vs {cm['opponent']}: banked "
              f"you {cm['my_points_this_week']} — them {cm['opponent_points_this_week']}")
        if cm.get("my_projected_this_week") is not None:
            print(f"  projected final: you {cm['my_projected_this_week']} — "
                  f"them {cm['opponent_projected_this_week']}")
        else:
            print("  projected final: (unavailable — needs SLEEPER_TOKEN)")
        tops = ", ".join(f"{p['name']} ({p.get('fpts_per_game') or p.get('proj_fpts_per_game') or '?'})"
                         for p in cm.get("opponent_top_players", []))
        print(f"  their danger men: {tops}")
    else:
        print("  (no head-to-head matchup found for this week)")
    if tc.get("standings"):
        print("  standings top 5:")
        for r in tc["standings"][:5]:
            print(f"    {r['rank']:>2}. {r['team'][:22]:22} {r['record']:>6}  pf={r['pts_for']}")

    # ---- 3b. this week: schedule, games left, lineup locks ----
    line()
    print("[3b] THIS WEEK — NBA schedule, games left, lineup locks (weekly lineups)")
    from sleeper.nba_schedule import load_week_schedule
    from brain.scouting import week_value
    sched = load_week_schedule()
    print(f"  {sched.summary()}")
    mine = (read.my_matchup(config.SLEEPER_LEAGUE_ID, week, roster["roster_id"]) or {}).get("mine") or {}
    played = {str(p) for p, v in (mine.get("players_points") or {}).items() if v}
    starters = {str(p) for p in (roster.get("starters") or [])}
    print(f"  {'player':22} {'team':5} {'slot':7} {'games left':>10} {'rest-of-week pts':>17}  lock")
    for c in sorted(cards, key=lambda c: -(week_value(c, sched.games_left(c.get('team'))) or 0)):
        gl = sched.games_left(c.get("team"))
        if c["player_id"] in played:
            lock = "LOCKED (has played)"
        elif sched.known and sched.started_this_week(c.get("team")):
            lock = "locked? (team tipped off)"
        else:
            lock = "movable"
        print(f"  {c['name'][:22]:22} {str(c.get('team')):5} "
              f"{'START' if c['player_id'] in starters else 'bench':7} "
              f"{('?' if gl is None else gl):>10} {week_value(c, gl):>17}  {lock}")
    if not sched.known:
        print("  ⚠️  schedule didn't load — the bot will rank by per-game value until it does.")

    # ---- 4. trades + DMs visible (counts only) ----
    line()
    print("[4] TRADES & DMs VISIBLE")
    if not config.SLEEPER_TOKEN:
        print("  (no SLEEPER_TOKEN set — skipping the authenticated trade/DM checks)")
    else:
        write = SleeperWrite(config.SLEEPER_TOKEN, config.SLEEPER_LEAGUE_ID, enable_writes=False)
        try:
            trades = write.get_trades(roster_id=roster["roster_id"])
            opens = [t for t in trades if t.get("status") in ("proposed", "pending")]
            incoming = [t for t in opens if str(t.get("creator")) != str(user_id)]
            print(f"  trades fetched: {len(trades)} total, {len(opens)} open, {len(incoming)} incoming to you")
            for t in incoming:
                print(f"    - tx {t.get('transaction_id')} status={t.get('status')} "
                      f"from creator {t.get('creator')} rosters={t.get('roster_ids')}")
        except Exception as e:
            print(f"  ⚠️  couldn't fetch trades: {e}")
        try:
            dms = write.my_dms()
            print(f"  DM threads: {len(dms)}")
            for d in dms[:8]:
                n = len(write.dm_messages(d["dm_id"], limit=200))
                print(f"    - {d.get('last_author_display_name') or d.get('dm_id')}: "
                      f"{n} messages in thread, last=\"{(d.get('last_message_text') or '')[:40]}\"")
        except Exception as e:
            print(f"  ⚠️  couldn't fetch DMs: {e}")

    line("=")
    print("SELFTEST DONE. If stats loaded and fpg numbers look sane, the bot can")
    print("see everything. If section [1] flagged unmatched stat keys, paste it.")
    line("=")


if __name__ == "__main__":
    main()
