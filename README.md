# Sleeper AI Manager 🏀🤖

An autonomous fantasy basketball manager for [Sleeper](https://sleeper.com),
powered by the Claude API. It sets weekly lineups, works the waiver wire,
manages IR, accepts/rejects/negotiates trades, and trash-talks in DMs — tuned to
your league's exact scoring rules.

Built for league **"Goon"** (`1411433449088155648`), an **18-team** NBA points
league with **weekly lineups** and a $100 FAAB budget.

---

## How this league works (and how the bot reasons about it)

**Weekly lineups.** There is one lineup per matchup week (Mon–Sun). A starting
slot earns *every* game its player plays that week, and each player locks into
his slot — starter or bench — at his first tip-off of the week, for the rest of
the week. So a player's lineup value is:

> **per-game fantasy points × games he has left this week**

Whether he plays *tonight* is irrelevant. (The bot used to think nightly and
start "whoever plays tonight" — those players then locked in for the week while
stars who played later sat. Fixed: see `set_lineup` below.)

**Two different scores.** Sleeper's matchup screen headlines the *projected*
final and win odds. The *banked* score is what's actually been scored so far.
Early in a week you can lead on banked points and still be the underdog. The bot
reads both and is told explicitly to act on the projection.

**Scoring.** Steals and blocks pay 2×, points only 0.5×, turnovers −1, with
double/triple-double bonuses. The bot scores every player by *this* league's
rules (pulled live from the league's `scoring_settings`), so disciplined
two-way players beat empty-calorie scorers.

---

## Commands

| Command | What happens |
|---|---|
| `lineup` | Fills this week's **unlocked** starting slots for maximum rest-of-week points; moves injured players to/from IR. |
| `manage` | Waiver claims (only legal drops), the empty-slot safety net, and incoming trades (all open offers re-checked daily). |
| `dm` / `dm-preview` | Reads DMs and replies in character; handles fresh trade offers and can close a negotiated deal. |
| `selftest` | Live **read-only** check: stats, projections, matchup, **this week's NBA schedule and lineup locks**, trades, DMs. Run this first after any deploy. |
| `scout` | Quick read-only league snapshot. |
| `introspect` | Diagnostic dump of the raw matchup/score data (state, per-week points, matchup_legs fields). |
| `draft` | Draft-night assistant (recommend-only). |

All are triggered from the **Actions** tab (`Sleeper AI Manager` workflow →
*Run workflow* → pick a command), or run locally as `python run.py <command>`.

---

## How each decision is made

The **code** does the parts that are math; **Claude** does the parts that need
judgment and live news. Every Claude call is wrapped so an API outage never
kills a run.

- **Lineup** (`autopilot/weekly_runner.py::set_lineup`). Loads this week's NBA
  schedule (`sleeper/nba_schedule.py`), works out who is already locked, and
  ranks every movable player by projected rest-of-week points. Claude checks
  injury/rest/role news via web search and can **veto** a player (with a reason)
  or nudge near-ties; it can't override a big gap in projected points. The
  ranked list is seated with augmenting paths, which is provably optimal for
  position-constrained slots. If Sleeper rejects the write because someone was
  locked, it retries once with every already-tipped-off player pinned.
- **Waivers.** Claude picks claims, but only from players that can actually be
  dropped (a starter locked into this week's lineup can't be). Bad drops are
  caught before they reach Sleeper. Any empty roster slot is always filled.
- **Trades.** Claude decides accept / negotiate / reject. If accepting would
  overfill the roster, Claude must name who to drop (from the least valuable,
  droppable players, max 2); the code validates them, confirms the offer is
  still open, drops, accepts, and only sends the "done" DM after Sleeper
  confirms the trade went through.
- **IR.** Injured players the brain flags are stashed on IR (freeing an active
  slot); healthy ones come back when there's room.

If the schedule can't be fetched, the bot ranks by per-game value instead. If
stats can't be fetched, it falls back to Claude's ordering. Nothing hard-fails.

---

## The two halves, and the risk

**Reading (safe).** Rosters, matchups, stats, projections and standings come
from Sleeper's public read API. The NBA schedule comes from NBA.com's public
schedule file (ESPN as a fallback).

**Writing (unofficial, your call).** Sleeper has **no official write API**.
Lineups, claims, IR moves, drops and trade responses go through the private
GraphQL endpoint the app itself uses — mutations verified against the community
SDK [`cameron-eth/sleeper-sdk`](https://github.com/cameron-eth/sleeper-sdk).
It's undocumented, can break anytime, and may violate Sleeper's Terms of
Service. You've accepted that.

---

## Setup (GitHub Actions)

Secrets (repo → Settings → Secrets and variables → Actions):

| Secret | Notes |
|---|---|
| `ANTHROPIC_API_KEY` | A dedicated key for this project. |
| `CLAUDE_MODEL` | Optional; defaults to `claude-opus-4-6`. |
| `SLEEPER_USERNAME`, `SLEEPER_LEAGUE_ID` | `iamclaude`, `1411433449088155648`. |
| `SLEEPER_TOKEN` | Your Sleeper session token. **Password-equivalent — only ever a secret, never committed.** |
| `ENABLE_WRITES` | `true` to act; anything else = dry-run (logs what it would do). |
| `ENABLE_TRADE_PROPOSALS` | `true` to let the DM negotiator send trade offers on its own. |
| `DISCORD_WEBHOOK_URL` | Optional; a ping for every decision. |

**Schedules** (`.github/workflows/`):
- `sleeper-bot.yml` — `manage` daily at 6:00am ET, then `lineup` at 7:00am ET.
- `sleeper-dms.yml` — the DM bot every 20 minutes. To keep Claude costs sane it
  only evaluates trade offers made in the last ~75 minutes; older open offers
  are re-checked once a day by `manage`.

Every run uploads `data/audit.log` (every write intent + outcome) as an artifact.

---

## Safety model

1. **Dry-run by default.** Without `ENABLE_WRITES=true` + a token, every write is
   logged, not sent.
2. **Never blind-retry an add/drop.** A retried roster move can double-submit.
   (A rejected *lineup* is safe to resubmit — it's a full replacement — so that
   one retry exists.) Network errors are logged and the run moves on.
3. **Validate before acting.** Drops are checked against lineup locks, trade
   drops against a short "least valuable" list, proposals against roster room.
4. **Honest DMs.** A trade confirmation is only sent after Sleeper confirms.
5. **Audit log** of every intent and outcome.

---

## Layout

```
run.py                       entry point (all commands)
config.py                    env + fallback scoring weights
selftest.py                  live read-only health check (run after deploys)
introspect.py                raw score-data diagnostic
sleeper/read_client.py       public read API
sleeper/write_client.py      unofficial GraphQL writes (lineup, IR, claims, drops, trades, DMs)
sleeper/nba_schedule.py      this week's NBA schedule: games left, who has tipped off
brain/scoring.py             stats -> this league's fantasy points
brain/scouting.py            player cards, weekly value, matchup context, lineup slotting
brain/claude_manager.py      Claude prompts (lineup/waiver/trade/negotiate/DM persona)
brain/notify.py              Discord + stdout notifications
autopilot/weekly_runner.py   lineup + waivers + trades + IR
autopilot/dm_handler.py      DM bot + trade negotiation
draft/draft_assistant.py     draft-night helper
```
