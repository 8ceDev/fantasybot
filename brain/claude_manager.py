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

SYSTEM = f"""You are an elite fantasy basketball general manager running a team \
autonomously in a 16-team Sleeper points league. You are decisive, value-driven, \
and you exploit the specific scoring rules.

{explain_weights()}

Roster format per team: PG, SG, G, SF, PF, F, C, UTIL, UTIL, then bench + 1 reserve.
Waivers use a $100 FAAB budget. Trade deadline is league week 18.

Rules of engagement:
- Prioritize players who accumulate steals, blocks, rebounds and assists, not \
just scoring, because of the scoring weights.
- Avoid turnover-heavy, low-efficiency volume scorers unless the counting stats \
truly justify it.
- Factor in injuries, back-to-backs, and role changes when you know them.
- Be realistic about FAAB: don't blow the budget early on marginal upgrades.
- When unsure, prefer the lower-risk move and say so.

You ALWAYS respond with a single valid JSON object and nothing else."""


def _ask(user_prompt: str, max_tokens: int = 2000) -> dict:
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
    Returns {"pick": player_id, "name": ..., "reason": ..., "backups": [...]}"""
    prompt = (
        "It's your turn to draft. Pick the best player for the long-term value "
        "of my team given my current roster and who's available.\n\n"
        f"My current roster:\n{json.dumps(context.get('your_roster', []), indent=2)}\n\n"
        f"Best available players (pre-scored by my league's rules, best first):\n"
        f"{json.dumps(context.get('available_top', [])[:40], indent=2)}\n\n"
        f"Pick number: {context.get('pick_number')}  |  Rounds left: {context.get('rounds_left')}\n\n"
        'Respond as JSON: {"pick": "<player_id>", "name": "<player name>", '
        '"reason": "<1-2 sentences>", "backups": ["<player_id>", "<player_id>"]}'
    )
    return _ask(prompt)


# ---------- LINEUP ----------
def lineup_decision(context: dict) -> dict:
    """context: {roster_positions, candidates: [{player_id,name,pos,status,note,proj}], slots}
    Returns {"starters": [player_id,...ordered by slot], "reserve": [...], "reason": ...}"""
    prompt = (
        "Set my optimal starting lineup for today. Fill each roster slot with the "
        "eligible player who maximizes expected fantasy points under my league's "
        "scoring. Bench anyone not playing today or ruled out.\n\n"
        f"Roster slots to fill (in order): {json.dumps(context.get('roster_positions', []))}\n\n"
        f"My players today:\n{json.dumps(context.get('candidates', []), indent=2)}\n\n"
        'Respond as JSON: {"starters": ["<player_id per slot, in slot order>"], '
        '"reserve": ["<player_id>"], "reason": "<short explanation>"}'
    )
    return _ask(prompt)


# ---------- WAIVERS ----------
def waiver_decision(context: dict) -> dict:
    """context: {your_roster, free_agents_top, trending_adds, faab_left, open_slots}
    Returns {"claims": [{"add","drop","faab","reason"}], "note": ...}"""
    prompt = (
        "Decide my waiver wire claims for this cycle. Only claim players who are a "
        "real upgrade. For each claim give the player to add, the player to drop "
        "(or null if I have an open slot), and a sensible FAAB bid from my remaining "
        "budget. It's fine to make zero claims.\n\n"
        f"My roster:\n{json.dumps(context.get('your_roster', []), indent=2)}\n\n"
        f"Top available free agents (pre-scored, best first):\n"
        f"{json.dumps(context.get('free_agents_top', [])[:30], indent=2)}\n\n"
        f"Trending adds across Sleeper:\n{json.dumps(context.get('trending_adds', [])[:15], indent=2)}\n\n"
        f"FAAB remaining: ${context.get('faab_left')}  |  Open roster slots: {context.get('open_slots')}\n\n"
        'Respond as JSON: {"claims": [{"add": "<player_id>", "drop": "<player_id or null>", '
        '"faab": <int>, "name": "<add name>", "reason": "<short>"}], "note": "<overall note>"}'
    )
    return _ask(prompt)


# ---------- TRADES ----------
def evaluate_trade(context: dict) -> dict:
    """context: {incoming_trade, my_roster, their_roster}
    Returns {"accept": bool, "reason": ..., "counter": {...} | null}"""
    prompt = (
        "Someone proposed a trade to me. Decide whether to accept it based on "
        "the value I give up vs. receive under my league's scoring and my roster "
        "needs. If it's close or slightly unfavorable but fixable, suggest a counter.\n\n"
        f"The proposed trade:\n{json.dumps(context.get('incoming_trade', {}), indent=2)}\n\n"
        f"My roster:\n{json.dumps(context.get('my_roster', []), indent=2)}\n\n"
        f"Their roster:\n{json.dumps(context.get('their_roster', []), indent=2)}\n\n"
        'Respond as JSON: {"accept": true/false, "reason": "<explanation>", '
        '"counter": {"send": ["<player_id>"], "receive": ["<player_id>"]} or null}'
    )
    return _ask(prompt)
