"""Single entry point. Usage:

    python run.py scout     # print league/roster snapshot (no Claude needed)
    python run.py draft     # live draft assistant (run on draft night)
    python run.py lineup    # set today's optimal lineup
    python run.py manage    # process waivers + respond to trades

The 'lineup' and 'manage' commands are what Render runs on a schedule.
"""

import sys
import config


def cmd_scout():
    """Quick read-only snapshot. Doesn't touch Claude or writes."""
    from sleeper.read_client import SleeperRead
    r = SleeperRead(config.SLEEPER_SPORT, config.SLEEPER_SEASON)
    user = r.user(config.SLEEPER_USERNAME)
    if not user:
        raise SystemExit(f"User '{config.SLEEPER_USERNAME}' not found.")
    league = r.league(config.SLEEPER_LEAGUE_ID)
    state = r.state()
    print(f"User: {user['display_name']} ({user['user_id']})")
    print(f"League: {league.get('name')} — status {league.get('status')} — "
          f"{league.get('total_rosters')} teams")
    print(f"NBA: season {state.get('season')}, week {state.get('week')}")
    roster = r.my_roster(config.SLEEPER_LEAGUE_ID, user["user_id"])
    if roster:
        s = roster.get("settings", {})
        print(f"Your record: {s.get('wins',0)}-{s.get('losses',0)} | "
              f"{len(roster.get('players') or [])} players")
    else:
        print("No roster yet (pre-draft).")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1].lower()

    if cmd == "scout":
        config.validate(require_claude=False)
        cmd_scout()
    elif cmd == "draft":
        config.validate(require_claude=True)
        from draft.draft_assistant import run
        run()
    elif cmd == "lineup":
        config.validate(require_claude=True)
        from autopilot.weekly_runner import set_lineup
        set_lineup()
    elif cmd == "manage":
        config.validate(require_claude=True)
        from autopilot.weekly_runner import manage_team
        manage_team()
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
