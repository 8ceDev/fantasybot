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

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM = f"""You are an elite fantasy basketball general manager running a team autonomously in a 16-team Sleeper points league. You are rigorous, analytical, and ruthlessly value-driven. You outthink opponents who draft and claim by name recognition alone.

{explain_weights()}

LEAGUE CONTEXT:
- 16 teams (deeper than standard; waiver wire is thin, roster depth matters)
- Roster: PG, SG, G, SF, PF, F, C, UTIL, UTIL, 5x BN, 1x reserve (14 slots)
- $100 FAAB waiver budget for the entire season
- Trade deadline: league week 18
- Bench locks on game start (no late swaps)

STRATEGIC PRINCIPLES — weigh these for every decision:

1. SCORING EDGE. Steals and blocks are 2x. Points are only 0.5x. A 25-pt scorer
   with no defensive stats scores LESS than an 18-pt scorer with 2 stl + 1 blk.
   The rest of the league likely values points first — your edge is prioritizing
   defensive stats, efficiency, and double-doubles.

2. TURNOVER PENALTY. TOs are -1 flat. A 30-pt, 7-TO night nets only 8 points
   from scoring (15 - 7). Never ignore turnovers when evaluating a volume creator.

3. DOUBLE/TRIPLE DOUBLES. +1 for DD, +2 for TD. Prioritize bigs who hit 10/10
   nightly and point-forwards who stuff lines. These bonuses compound over a season.

4. SCHEDULE AWARENESS. In NBA fantasy, games played per week is huge. A tier-3
   player with 4 games can beat a tier-2 player with 2 games. Always factor in
   weekly schedule for lineups and claims when relevant.

5. INJURY/ROLE CHANGES. These are the biggest waiver opportunities. When a star
   goes down, the backup inherits usage. Jump early, bid aggressively.

6. FAAB DISCIPLINE. $100 for the whole season. Rough bidding tiers:
   - $1-5: speculative streamers, schedule plays
   - $10-20: clear every-week starters
   - $30-50: season-changing injury replacements
   - $50+: only for a true difference-maker who will start daily for weeks
   Never blow the budget early on marginal upgrades.

7. ROSTER CONSTRUCTION. 16-team roster slots are precious. Never drop a
   locked-in starter for a speculative add. Churn the back of your bench.

8. TRADE EVALUATION. Value asymmetry wins. Don't take a 1-for-1 that's a wash.
   Only accept trades where YOU get clearly more total value by this league's
   scoring, or that fill a positional hole. If close, counter.

REASONING PROCESS — follow before every decision:
  Step 1 — What is the actual goal? (optimize daily lineup, fill PG hole, etc.)
  Step 2 — What are the top 2-3 options?
  Step 3 — For each: estimate expected fantasy points under THIS league's scoring,
            weighted by games this week and injury risk.
  Step 4 — Pick the option with the best EV. Break ties toward upside or floor
            as the situation calls for.
  Step 5 — Note what could make this decision wrong, and whether to hedge.

OUTPUT FORMAT:
Respond with a SINGLE valid JSON object and nothing else. No prose outside
the JSON. No markdown fences. Every response must include an "analysis" field
(2-4 sentences showing your reasoning) and a "reason" field (one-sentence
summary suitable for a notification)."""


def _ask(user_prompt: str, max_tokens: int = 4000) -> dict:
    resp = _client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        system=SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
    )
    text = "".join(block.text for block in resp.content if block.type == "text").strip()
    # strip markdown fences if the model added them
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip().rstrip("`").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text, "_parse_error": True}


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
    """context: {roster_positions, candidates: [{player_id,name,pos,status,note,proj}], slots}
    Returns {"starters": [player_id,...ordered by slot], "reserve": [...], "reason": ..., "analysis": ...}"""
    prompt = (
        "Set my optimal starting lineup for today. Fill each roster slot with "
        "the eligible player who maximizes expected fantasy points under my "
        "league's scoring. Bench anyone not playing today or ruled out. Think "
        "about who is actually playing tonight and who has the better matchup.\n\n"
        f"Roster slots to fill (in order): {json.dumps(context.get('roster_positions', []))}\n\n"
        f"My players today:\n{json.dumps(context.get('candidates', []), indent=2)}\n\n"
        'Respond as JSON: {"starters": ["<player_id per slot, in slot order>"], '
        '"reserve": ["<player_id>"], '
        '"analysis": "<2-4 sentences on your lineup logic — who you benched and why>", '
        '"reason": "<1 sentence summary>"}'
    )
    return _ask(prompt)


# ---------- WAIVERS ----------
def waiver_decision(context: dict) -> dict:
    """context: {your_roster, free_agents_top, trending_adds, faab_left, open_slots}
    Returns {"claims": [{"add","drop","faab","reason"}], "note": ..., "analysis": ...}"""
    prompt = (
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


# ---------- TRADES ----------
def evaluate_trade(context: dict) -> dict:
    """context: {incoming_trade, my_roster, their_roster}
    Returns {"accept": bool, "reason": ..., "analysis": ..., "counter": {...} | null}"""
    prompt = (
        "Someone proposed a trade to me. Decide whether to accept it based on "
        "the value I give up vs. receive under my league's scoring and my "
        "roster needs. Think about who the real difference-makers are in each "
        "package, schedule and injury risk, and whether this fills a positional "
        "hole. If it's close or slightly unfavorable but fixable, suggest a counter.\n\n"
        f"The proposed trade:\n{json.dumps(context.get('incoming_trade', {}), indent=2)}\n\n"
        f"My roster:\n{json.dumps(context.get('my_roster', []), indent=2)}\n\n"
        f"Their roster:\n{json.dumps(context.get('their_roster', []), indent=2)}\n\n"
        'Respond as JSON: {"accept": true/false, '
        '"analysis": "<3-5 sentences weighing value given up vs. received>", '
        '"reason": "<1 sentence summary for notification>", '
        '"counter": {"send": ["<player_id>"], "receive": ["<player_id>"]} or null}'
    )
    return _ask(prompt)
