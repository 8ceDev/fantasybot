"""Turns raw per-game stats into fantasy points under YOUR league's rules.

This is the quiet edge. Most people eyeball "good players." Your league pays
2x for steals and blocks, 0.5 for points, -1 per turnover, and bonuses for
double/triple-doubles. A high-usage chucker who turns it over is worth less
here than a defensive big who racks up stocks and boards. This module scores
every player on that exact basis so the Claude brain ranks from a correct
baseline instead of generic name recognition.
"""

from config import SCORING_WEIGHTS

# Per-game stat keys we expect from a projection/stat line. Missing keys = 0.
STAT_KEYS = ["pts", "reb", "ast", "stl", "blk", "to", "tpm"]


def fantasy_points(statline: dict) -> float:
    """Score a single game or per-game average line in league points."""
    s = statline or {}
    total = 0.0
    for key in STAT_KEYS:
        total += SCORING_WEIGHTS.get(key, 0.0) * float(s.get(key, 0) or 0)

    # double/triple double bonuses: count categories >= 10
    cats_over_10 = sum(
        1 for k in ("pts", "reb", "ast", "stl", "blk")
        if float(s.get(k, 0) or 0) >= 10
    )
    if cats_over_10 >= 3:
        total += SCORING_WEIGHTS.get("td", 0.0)
    elif cats_over_10 == 2:
        total += SCORING_WEIGHTS.get("dd", 0.0)

    # scoring bonuses
    pts = float(s.get("pts", 0) or 0)
    if pts >= 50:
        total += SCORING_WEIGHTS.get("bonus_pt_50p", 0.0)
    if pts >= 40:
        total += SCORING_WEIGHTS.get("bonus_pt_40p", 0.0)

    # penalties
    total += SCORING_WEIGHTS.get("ff", 0.0) * float(s.get("ff", 0) or 0)
    total += SCORING_WEIGHTS.get("tf", 0.0) * float(s.get("tf", 0) or 0)
    return round(total, 2)


def rank_players(player_lines: dict) -> list:
    """player_lines: {player_id: per_game_statline}. Returns list of
    (player_id, projected_fantasy_points) sorted best-first."""
    scored = [(pid, fantasy_points(line)) for pid, line in player_lines.items()]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


# Per-game stat categories we surface on a player card (raw averages).
PER_GAME_CATS = ["pts", "reb", "ast", "stl", "blk", "to", "tpm"]


def score_statline(stats: dict, weights: dict = None) -> float:
    """Canonical scoring: sum weight[k] * stats[k] over every scoring key.

    This is exactly how Sleeper tallies fantasy points — its scoring_settings
    keys ARE stat keys — so applied to a SEASON-TOTAL line (which already carries
    dd/td and bonus-game tallies as their own keys) it reproduces the league's
    real total. Prefer this over fantasy_points() whenever you have a true
    Sleeper statline; fantasy_points() is the heuristic for bare per-game lines
    that lack dd/td tallies.
    """
    w = weights or SCORING_WEIGHTS
    if not stats:
        return 0.0
    total = 0.0
    for k, wt in w.items():
        v = stats.get(k)
        if v is None:
            continue
        try:
            total += float(wt) * float(v)
        except (TypeError, ValueError):
            continue
    return round(total, 2)


def games_played(stats: dict) -> int:
    for k in ("gp", "gms_active", "g"):
        v = (stats or {}).get(k)
        if v:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    return 0


def per_game(stats: dict, weights: dict = None) -> tuple:
    """Return (fantasy_points_per_game, games_played) from a season-total line."""
    gp = games_played(stats)
    total = score_statline(stats, weights)
    return (round(total / gp, 2) if gp else 0.0, gp)


def per_game_cats(stats: dict) -> dict:
    """Raw per-game averages for the headline categories, so the brain can see
    the shape of a player's line (not just the summed fantasy number)."""
    gp = games_played(stats)
    if not gp:
        return {}
    out = {}
    for k in PER_GAME_CATS:
        v = (stats or {}).get(k)
        if v is None:
            continue
        try:
            out[k] = round(float(v) / gp, 1)
        except (TypeError, ValueError):
            continue
    return out


def explain_weights() -> str:
    """Human-readable summary the brain can include in its prompt context."""
    return (
        "League scoring (per stat): "
        "points 0.5, rebounds 1, assists 1, steals 2, blocks 2, turnovers -1, "
        "3PM 0.5, double-double +1, triple-double +2, 40pt game +2, 50pt game +2, "
        "flagrant/technical -2 each. Steals and blocks are disproportionately "
        "valuable; turnover-prone volume scorers are overrated here."
    )
