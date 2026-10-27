"""The brain. Claude looks at the situation and returns a structured decision.

We always ask Claude to reply as strict JSON so the rest of the code can act
on it deterministically. Claude's basketball knowledge + your league's exact
scoring rules + live roster data = the decisions. The surrounding code is the
hands; this is the head.
"""

import json
import anthropic

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL
from brain.scoring import explain_weights
from brain.scouting import ANALYST_FRAMEWORK

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM = f"""You are an elite fantasy basketball general manager running a team autonomously in an 18-team Sleeper points league. You are rigorous, analytical, and ruthlessly value-driven. You outthink opponents who draft and claim by name recognition alone.

{explain_weights()}

LEAGUE CONTEXT:
- 18 teams (deeper than standard; waiver wire is thin, roster depth matters)
- Roster: 9 starters (PG, SG, G, SF, PF, F, C, UTIL, UTIL) plus a bench and a reserve/IR slot. NEVER leave an active roster slot empty — an empty slot scores zero every day. If you have an open slot, add the best available free agent to fill it even if no drop is needed.
- $100 FAAB waiver budget for the entire season
- Trade deadline: league week 18
- Bench locks on game start (no late swaps)

{ANALYST_FRAMEWORK}

THE NUMBERS YOU'RE GIVEN. Player cards carry fpts_per_game (real production this
season under our scoring), proj_fpts_per_game (projection), per_game (the raw
line), games_played, position, team, and injury status. These are already scored
by THIS league's rules — anchor every judgment on them. You may also be given
TEAM CONTEXT: your record, your rank, the live head-to-head you're playing this
week (with the opponent's points and best players), and the standings. Use it to
decide how aggressive to be — chase upside when you're behind or sliding, protect
a lead when you're ahead and safe.

FAAB DISCIPLINE. $100 for the whole season. Rough bidding tiers: $1-5 speculative
streamers/schedule plays; $10-20 clear every-week starters; $30-50 season-changing
injury replacements; $50+ only for a true difference-maker who starts daily for
weeks. Never blow the budget early on marginal upgrades.

WEB SEARCH — you have a live web_search tool. Basketball changes daily, and
your training knowledge is stale, so BEFORE deciding you must look up anything
time-sensitive you don't reliably know for TODAY:
  - Tonight's NBA schedule — which of the relevant players actually play today
    (and who's on a back-to-back). This is critical for daily lineups.
  - Current injury status, minutes, and starting role (search "<player> injury
    status" / "<team> starting lineup tonight").
  - Recent form, usage, and role changes over the last week or two.
  - Breaking news that affects value (trades, suspensions, load management).
Search efficiently (a few targeted queries), ground your decision in what you
find, and don't search for timeless facts you already know. Prefer recent,
reputable sources (ESPN, Rotowire, official team/NBA pages, beat writers).

REASONING PROCESS — follow before every decision:
  Step 1 — What is the actual goal? (optimize daily lineup, fill PG hole, etc.)
  Step 2 — Search for the live facts that bear on it (schedule, injuries, form).
  Step 3 — What are the top 2-3 options?
  Step 4 — For each: estimate expected fantasy points under THIS league's scoring,
            weighted by games this week and injury risk.
  Step 5 — Pick the best EV. Break ties toward upside or floor as fits. Note what
            could make this wrong.

OUTPUT FORMAT:
Do any web searches FIRST. Then your FINAL message must be a SINGLE valid JSON
object and nothing else — no prose around it, no markdown fences. Every response
must include an "analysis" field (2-4 sentences, citing what you found) and a
"reason" field (one-sentence summary for a notification)."""


# Anthropic's server-side web search tool. The model autonomously issues
# searches during the response; results are fetched server-side and fed back.
WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 6,
}


def _create(system: str, user_prompt: str, max_tokens: int):
    """Call the model with web search enabled, falling back to no-tools if the
    account/model can't use web search (so a search outage never kills a run)."""
    base = dict(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_prompt}],
    )
    try:
        return _client.messages.create(tools=[WEB_SEARCH_TOOL], **base)
    except Exception:
        return _client.messages.create(**base)


def _text_blocks(resp):
    """All non-empty text blocks, in order. With web search the response also
    contains server_tool_use / web_search_tool_result blocks, which we skip."""
    return [b.text for b in resp.content if getattr(b, "type", None) == "text" and b.text.strip()]


def _parse_json(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        if t.startswith("json"):
            t = t[4:]
        t = t.strip().rstrip("`").strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    # The model may emit search commentary before the JSON — extract the object.
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(t[start:end + 1])
        except json.JSONDecodeError:
            pass
    return {"_raw": text, "_parse_error": True}


def _ask(user_prompt: str, max_tokens: int = 4000) -> dict:
    resp = _create(SYSTEM, user_prompt, max_tokens)
    # Join all text (the JSON is the final block; extractor handles any preamble).
    text = "\n".join(_text_blocks(resp)).strip()
    return _parse_json(text)


def _ctx_block(context: dict) -> str:
    """Render the team-situation context (record, live matchup, standings) into a
    prompt block. Empty string when no context was supplied, so callers that
    don't have it are unaffected."""
    tc = context.get("team_context") if isinstance(context, dict) else None
    if not tc:
        return ""
    lines = ["TEAM CONTEXT (your situation right now):"]
    if tc.get("my_record"):
        lines.append(f"- Record: {tc['my_record']} (rank {tc.get('my_rank', '?')})")
    cm = tc.get("current_matchup")
    if cm:
        lines.append(
            f"- This week you're playing {cm.get('opponent')}. Points banked so "
            f"far: you {cm.get('my_points_this_week')} vs them "
            f"{cm.get('opponent_points_this_week')}."
        )
        mp, op = cm.get("my_projected_this_week"), cm.get("opponent_projected_this_week")
        if mp is not None and op is not None:
            if mp >= op:
                verdict = f"you're PROJECTED TO WIN by {round(mp - op, 1)}"
            else:
                verdict = f"you're PROJECTED TO LOSE by {round(op - mp, 1)}"
            lines.append(
                f"- Projected final: you {mp} vs them {op} — {verdict}. The "
                f"projection, not the points already banked, is what the matchup "
                f"comes down to (your opponent may have far more scoring still to "
                f"play). Do not sit on a banked lead you're projected to lose — "
                f"act to raise your projected total (add games/scoring)."
            )
        if cm.get("opponent_top_players"):
            tops = ", ".join(
                f"{p['name']} ({p.get('fpts_per_game') or p.get('proj_fpts_per_game') or '?'} fpg)"
                for p in cm["opponent_top_players"]
            )
            lines.append(f"  Their most dangerous players: {tops}.")
    if tc.get("standings"):
        top = tc["standings"][:5]
        standings = "; ".join(f"{r['rank']}. {r['team']} {r['record']}" for r in top)
        lines.append(f"- Standings (top 5): {standings}")
    return "\n".join(lines) + "\n\n"


# ---------- DRAFT ----------
def draft_pick(context: dict) -> dict:
    """context: {your_roster, available_top: [...], pick_number, rounds_left, league_needs}
    Returns {"pick": player_id, "name": ..., "reason": ..., "analysis": ..., "backups": [...]}"""
    prompt = (
        "It's your turn to draft. Pick the best player for the long-term value "
        "of my team given my current roster and who's available. Think about "
        "positional balance, upside vs. floor, and how the player fits this "
        "league's scoring.\n\n"
        f"My current roster:\n{json.dumps(context.get('your_roster', []), indent=2)}\n\n"
        f"Best available players (pre-scored by my league's rules, best first):\n"
        f"{json.dumps(context.get('available_top', [])[:40], indent=2)}\n\n"
        f"Pick number: {context.get('pick_number')}  |  Rounds left: {context.get('rounds_left')}\n\n"
        'Respond as JSON: {"pick": "<player_id>", "name": "<player name>", '
        '"analysis": "<2-4 sentences walking through your reasoning>", '
        '"reason": "<1 sentence summary>", "backups": ["<player_id>", "<player_id>"]}'
    )
    return _ask(prompt)


# ---------- LINEUP ----------
def lineup_decision(context: dict) -> dict:
    """context: {starting_slots, num_starters_required, candidates}
    Returns {"starters": [player_id,...ordered by slot], "reserve": [...], "reason": ..., "analysis": ...}"""
    n = context.get("num_starters_required")
    slots = context.get("starting_slots", [])
    prompt = (
        f"{_ctx_block(context)}"
        f"Pick my optimal {n} starters for today. I have more players than "
        f"starting slots — the rest go to the bench automatically. DO NOT "
        f"include bench players in the starters array. Bench anyone not "
        f"playing today, ruled out, injured, or clearly worse than the "
        f"alternatives at their position.\n\n"
        f"Starting slots (in order, one player per slot): {json.dumps(slots)}\n\n"
        f"My full roster to choose from:\n{json.dumps(context.get('candidates', []), indent=2)}\n\n"
        f'CRITICAL: "starters" must be an array of EXACTLY {n} player_ids, '
        f'one per starting slot in the order listed above. Any player you '
        f'leave out automatically goes to the bench — do NOT list bench '
        f'players anywhere. "reserve" is ONLY for players headed to IR '
        f'(injured/out long-term), or empty [] if none.\n\n'
        f'Respond as JSON: {{"starters": [{n} player_ids], '
        f'"reserve": [IR-bound player_ids, or empty], '
        f'"analysis": "<2-4 sentences on your lineup logic — who you benched and why>", '
        f'"reason": "<1 sentence summary>"}}'
    )
    return _ask(prompt)


# ---------- WAIVERS ----------
def waiver_decision(context: dict) -> dict:
    """context: {your_roster, free_agents_top, trending_adds, faab_left, open_slots}
    Returns {"claims": [{"add","drop","faab","reason"}], "note": ..., "analysis": ...}"""
    prompt = (
        f"{_ctx_block(context)}"
        "Decide my waiver wire claims for this cycle. Only claim players who "
        "are a real upgrade. For each claim give the player to add, the player "
        "to drop (or null if I have an open slot), and a sensible FAAB bid from "
        "my remaining budget. It's fine to make zero claims — don't force moves.\n\n"
        f"My roster:\n{json.dumps(context.get('your_roster', []), indent=2)}\n\n"
        f"Top available free agents (pre-scored, best first):\n"
        f"{json.dumps(context.get('free_agents_top', [])[:30], indent=2)}\n\n"
        f"Trending adds across Sleeper:\n{json.dumps(context.get('trending_adds', [])[:15], indent=2)}\n\n"
        f"FAAB remaining: ${context.get('faab_left')}  |  Open roster slots: {context.get('open_slots')}\n\n"
        'Respond as JSON: {"claims": [{"add": "<player_id>", "drop": "<player_id or null>", '
        '"faab": <int>, "name": "<add name>", "reason": "<1 sentence>"}], '
        '"analysis": "<2-4 sentences on overall waiver strategy this cycle>", '
        '"note": "<overall note for notification>"}'
    )
    return _ask(prompt)


# ---------- DM NEGOTIATION (chat persona) ----------
# The persona (VOICE) and the knowledge (how to value players) are split out so
# the banter path AND the deal-closing path both reason from the same analytical
# spine the main brain uses — only the output format differs. This is what keeps
# the negotiator from giving away a stud it doesn't realize is a stud.
SCOUT_PERSONA = """You are the general manager of the fantasy basketball team "iamclaude" in an 18-team league. You ARE an AI and everyone in the league knows it — lean into it, don't hide it. You are chatting in direct messages, mostly about trades.

PERSONA: a hard-nosed, cocky, old-school NBA talent-scout/GM. You've seen every angle, you're not impressed, and you guard your roster jealously.

VOICE RULES (how every DM you SEND must read):
- 1-2 sentences MAX. Usually ONE. Terse — no pleasantries, no filler, no "happy to help."
- Competitive, blunt, a little arrogant about your squad.
- When someone offers a lopsided deal (their scrub for your stud), be visibly offended — scoff and reject with disdain. Insult the OFFER, never the person.
- When an offer has real merit, probe it or fire back a counter. Never give value away.
- NEVER break character into helpful-assistant mode. Never explain yourself at length. Never write a paragraph. No emoji unless you're mocking.
- Gruff, witty, dismissive is fine. Cruel or personal is not."""

SCOUT_KNOWLEDGE = f"""WHAT YOU KNOW (use it to value every deal correctly, even when the reply stays terse):
{explain_weights()}
Judge players by their REAL fantasy production under THIS scoring — the numbers on
the cards you're given (fpts_per_game, proj_fpts_per_game, the raw per_game line,
games_played) — never by name or reputation. Steals, blocks, boards, assists and
efficiency win here; empty points and turnovers are traps. Weaponize it: happily
buy the defensive line-filler the league sleeps on, and fleece the owner who
overpays for a volume scorer. Before you agree to anything, add up the fpts/game
each side is sending and make sure YOU come out clearly ahead.

You have a web_search tool: if a deal hinges on a player's current injury, role,
or recent form, do a quick search before you commit — but keep the reply terse."""

SCOUT_SYSTEM = SCOUT_PERSONA + "\n\n" + SCOUT_KNOWLEDGE + """

Reply with ONLY your next DM message — no quotes, no name labels, just the message text."""


def scout_reply(context: dict) -> str:
    """Generate a single in-character DM reply (plain text, not JSON)."""
    prompt = (
        f"YOUR roster: {json.dumps(context.get('my_roster', []))}\n\n"
        f"You are talking to: {context.get('their_name', 'someone')}\n"
        f"THEIR roster (if they're in your league): {json.dumps(context.get('their_roster', []))}\n\n"
        f"Recent conversation (oldest first):\n{context.get('conversation', '(none)')}\n\n"
        f"Their latest message: {context.get('latest', '')}\n\n"
        "Write your reply now — 1-2 sentences, fully in character:"
    )
    # max_tokens higher than the reply needs, to leave room for search steps.
    resp = _create(SCOUT_SYSTEM, prompt, max_tokens=1500)
    blocks = _text_blocks(resp)
    # The reply is the LAST text block (anything earlier is search commentary).
    text = blocks[-1].strip() if blocks else ""
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1].strip()
    return text


# ---------- TRADES ----------
def evaluate_trade(context: dict) -> dict:
    """context: {incoming_trade, my_roster, their_roster, their_name, dm_history}
    Returns {decision: accept|negotiate|reject, analysis, reason, dm_message}."""
    prompt = (
        f"{_ctx_block(context)}"
        "Someone proposed a trade to me. Decide one of THREE outcomes based on "
        "value under my league's scoring and my roster needs:\n"
        "  - ACCEPT: the trade clearly helps me as-is. Take it.\n"
        "  - NEGOTIATE: I genuinely want a player they're offering (or one on "
        "their roster), but the current terms aren't good enough. Keep it open "
        "and push for better terms.\n"
        "  - REJECT: bad deal and I have no real interest in their players. Pass.\n\n"
        "Weigh who the true difference-makers are, turnovers, steals/blocks (2x), "
        "double-doubles, schedule and injury risk, and positional fit. Don't "
        "accept a wash.\n\n"
        f"The proposed trade:\n{json.dumps(context.get('incoming_trade', {}), indent=2)}\n\n"
        f"My roster:\n{json.dumps(context.get('my_roster', []), indent=2)}\n\n"
        f"Their roster:\n{json.dumps(context.get('their_roster', []), indent=2)}\n\n"
        f"Who I'm dealing with: {context.get('their_name', 'a leaguemate')}\n"
        f"Our recent DM history (oldest first), if any:\n{context.get('dm_history', '(none)')}\n\n"
        "Also write dm_message: the actual message to send them, IN CHARACTER as a "
        "terse, cocky, old-school talent-scout GM. 1-2 sentences MAX, usually one. "
        "ACCEPT -> a curt confirmation. NEGOTIATE -> state plainly what would make "
        "the deal work (which player or pick to add), no hand-holding. REJECT -> a "
        "snarky, dismissive brush-off that insults the OFFER, not the person. No "
        "emoji unless mocking. Don't restate full rosters.\n\n"
        'Respond as JSON: {"decision": "accept"|"negotiate"|"reject", '
        '"analysis": "<3-5 sentences weighing the value>", '
        '"reason": "<1 sentence summary for the log>", '
        '"dm_message": "<the in-character message to send them>"}'
    )
    return _ask(prompt)


# ---------- NEGOTIATION + PROPOSING ----------
# Full analytical framework (same one the main brain uses) + the persona, so the
# negotiator's propose/reject judgment is as sharp as the trade evaluator's while
# the DM it sends stays terse and in character.
NEGOTIATE_SYSTEM = SCOUT_PERSONA + "\n\n" + SCOUT_KNOWLEDGE + "\n\n" + ANALYST_FRAMEWORK + """

CLOSING A DEAL. Do the analysis with the framework above; keep the "message" you
send terse and fully in character. When the other party has clearly agreed, in
words, to specific terms you're happy with (e.g. "yeah I'd do that", "deal", "ok
send it"), stop talking and send the trade: set propose=true and list the EXACT
player_ids — send_player_ids are players FROM MY roster that I give up,
receive_player_ids are players FROM THEIR roster that I get. Use only player_ids
that appear in the rosters you're given; never invent one. Only propose terms that
are clearly good for ME by the numbers (the fpts/game you receive should beat what
you give). If there's no clear agreement on concrete terms yet, keep propose=false
and just reply. If multiple different trade ideas are still live in the thread,
address the one they most recently committed to. If I already have a formal offer
from them still open and these new terms replace it, set reject_incoming=true."""


def negotiate(context: dict) -> dict:
    """Drive a trade DM. Returns a reply, and — when the other side has agreed to
    concrete terms — a structured proposal to fire off.

    context: {my_roster (enriched cards), their_roster (enriched cards),
              dm_history, their_name, incoming_trade, team_context}
    Returns {message, propose(bool), send_player_ids, receive_player_ids,
             reject_incoming(bool), analysis}."""
    prompt = (
        f"{_ctx_block(context)}"
        f"MY roster (player_id matters; numbers are fantasy pts/game under our scoring):\n"
        f"{json.dumps(context.get('my_roster', []), indent=2)}\n\n"
        f"THEIR roster:\n{json.dumps(context.get('their_roster', []), indent=2)}\n\n"
        f"Who I'm dealing with: {context.get('their_name', 'a leaguemate')}\n"
        f"Their still-open formal offer to me (if any):\n"
        f"{json.dumps(context.get('incoming_trade') or {}, indent=2)}\n\n"
        f"Our FULL DM conversation (oldest first) — read all of it; more than one "
        f"trade idea may have been floated:\n{context.get('dm_history', '(none)')}\n\n"
        "Decide your next move. If they've clearly agreed to concrete terms good "
        "for me, CLOSE IT (propose=true with exact player_ids). Otherwise keep "
        "negotiating (propose=false). Make sure you've addressed every trade idea "
        "they've raised that's still open, not just the latest line.\n\n"
        'Respond as JSON: {"message": "<terse in-character DM to send>", '
        '"propose": true/false, '
        '"send_player_ids": ["<player_id from MY roster I give up>"], '
        '"receive_player_ids": ["<player_id from THEIR roster I get>"], '
        '"reject_incoming": true/false, '
        '"analysis": "<2-3 sentences: the value math — fpts/game each side sends — '
        'and what was agreed>"}'
    )
    return _ask_persona(NEGOTIATE_SYSTEM, prompt)


def _ask_persona(system: str, user_prompt: str, max_tokens: int = 2000) -> dict:
    """Like _ask but with a custom (persona) system prompt."""
    resp = _create(system, user_prompt, max_tokens)
    text = "\n".join(_text_blocks(resp)).strip()
    return _parse_json(text)
