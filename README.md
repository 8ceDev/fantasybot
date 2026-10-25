# Sleeper AI Manager 🏀🤖

An autonomous fantasy basketball manager for [Sleeper](https://sleeper.com),
powered by the Claude API. It drafts, sets daily lineups, works the waiver wire,
and accepts/rejects trades — tuned to your league's exact scoring rules.

Built for league **"Goon"** (`1411433449088155648`), a 16-team NBA points league.

---

## What it does

| Command | What happens |
|---|---|
| `python run.py scout` | Prints a read-only snapshot of your league. No API key needed. Good first test. |
| `python run.py draft` | Live draft-night assistant. Watches the draft and tells you who to pick each turn. |
| `python run.py lineup` | Sets today's optimal starting lineup. |
| `python run.py manage` | Processes waiver claims and responds to incoming trades. |

The brain is Claude. The hands are Sleeper's API. A scoring module
(`brain/scoring.py`) first translates raw stats into *your* league's points, so
Claude ranks from a correct baseline — in this league steals and blocks are
worth 2x and turnovers hurt, so disciplined two-way players beat empty-calorie
scorers.

---

## The two halves, and the risk

**Reading (safe).** Everything the bot *sees* comes from Sleeper's public,
documented, read-only API. No account access, no risk.

**Writing (unofficial, your call).** Sleeper has **no official write API**.
Setting lineups, claiming players, and responding to trades go through the
private GraphQL endpoint the app itself uses, reverse-engineered by the
community. It's undocumented, can break anytime, and may violate Sleeper's
Terms of Service. You've accepted that. The code is still built so you're never
flying blind — see Safety below.

---

## Setup

```bash
git clone <your-repo-url> sleeper-ai-manager
cd sleeper-ai-manager
pip install -r requirements.txt
cp .env.example .env         # then edit .env
python run.py scout          # confirm it sees your league
```

Fill in `.env`:
- `ANTHROPIC_API_KEY` — a **dedicated** key for this project (so you can revoke it alone).
- `SLEEPER_LEAGUE_ID`, `SLEEPER_USERNAME` — already pre-filled for you.
- `SLEEPER_TOKEN` — leave blank until you're ready to let it act (see below).
- `ENABLE_WRITES` — leave `false` until you trust it.
- `DISCORD_WEBHOOK_URL` — optional but recommended; you get a ping every time the bot thinks or acts.

### Getting your write token (only when ready)

The token is the `authorization` bearer value Sleeper's own web app sends.
Open sleeper.com logged in → DevTools → Network tab → do any action → find a
request to `sleeper.com/graphql` → copy the `authorization` header value into
`SLEEPER_TOKEN`. It's tied to your login; treat it like a password and never
commit it.

> ⚠️ **Before `ENABLE_WRITES=true`:** the write mutations in
> `sleeper/write_client.py` are marked `# VERIFY`. Confirm the exact GraphQL
> operation names/fields against a current community SDK
> (`cameron-eth/sleeper-sdk`) or by inspecting that same Network tab. That one
> file is the only thing standing between "recommends" and "acts."

---

## Safety model

The write layer is defensive on purpose:

1. **Dry-run by default.** With `ENABLE_WRITES=false` (or no token) the bot makes
   every decision and logs exactly what it *would* do — but submits nothing.
   Run it this way for a few days first.
2. **Re-read before write.** It re-fetches your live roster right before
   submitting, so a change you made on your phone won't get clobbered.
3. **No auto-retry on writes.** A retried roster move can double-submit, so on a
   network error it logs and stops rather than retrying.
4. **Audit log.** Every intent + outcome is appended to `data/audit.log`.
5. **Draft is recommend-only.** On draft night it tells you the pick; you tap it.
   (Auto-pick hook exists but is left off.)

**Recommended rollout:** `scout` → run `lineup`/`manage` in dry-run and read the
Discord pings for a week → verify the write mutations → set a token and flip
`ENABLE_WRITES=true`.

---

## Deploying to Render

`render.yaml` defines two cron jobs:
- `sleeper-lineup` — daily 7:00am ET, sets the day's lineup.
- `sleeper-waivers-trades` — daily 6:00am ET, waivers + trades.

Push this repo to GitHub, create a new **Blueprint** in Render pointing at it,
and set the env vars (same as `.env`) in the Render dashboard — never commit
`.env`. The draft assistant is interactive, so run that one locally on draft
night, not on Render.

---

## Layout

```
run.py                     entry point (scout / draft / lineup / manage)
config.py                  env + your league's scoring weights
sleeper/read_client.py     public read API (safe)
sleeper/write_client.py    unofficial write API (the # VERIFY file)
brain/scoring.py           stats -> your league's fantasy points
brain/claude_manager.py    Claude decisions (draft/lineup/waiver/trade)
brain/notify.py            Discord + stdout notifications
draft/draft_assistant.py   live draft-night helper
autopilot/weekly_runner.py lineup + waivers + trades
render.yaml                scheduled jobs
```

---

## A note on the trade-chatbot idea

You don't need a separate site or email for trades — Sleeper's built-in trade
system delivers offers straight to your account, and `manage` already evaluates
and responds to them. A chat UI where leaguemates haggle with your AI before
formally proposing is pure (hilarious) bonus; it'd reuse `brain/claude_manager.py`
with a roster-aware negotiator prompt. Say the word and it's a weekend add-on.
