"""Central configuration. Everything reads from environment variables so the
same code runs locally and on Render without edits."""

import os
from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# ---- Claude ----
ANTHROPIC_API_KEY = _get("ANTHROPIC_API_KEY")
CLAUDE_MODEL = _get("CLAUDE_MODEL", "claude-opus-4-6")

# ---- Sleeper identity ----
SLEEPER_USERNAME = _get("SLEEPER_USERNAME", "iamclaude")
SLEEPER_LEAGUE_ID = _get("SLEEPER_LEAGUE_ID", "1411433449088155648")
SLEEPER_SPORT = _get("SLEEPER_SPORT", "nba")
SLEEPER_SEASON = _get("SLEEPER_SEASON", "2026")

# ---- Write side ----
SLEEPER_TOKEN = _get("SLEEPER_TOKEN")
ENABLE_WRITES = _get("ENABLE_WRITES", "false").lower() == "true"

# Letting the bot PROPOSE trades on its own (give players away) is the riskiest
# capability it has, so it's gated behind its own flag — separate from, and in
# addition to, ENABLE_WRITES. Leave false until you've watched it in preview.
ENABLE_TRADE_PROPOSALS = _get("ENABLE_TRADE_PROPOSALS", "false").lower() == "true"

# ---- Notifications ----
DISCORD_WEBHOOK_URL = _get("DISCORD_WEBHOOK_URL")

# ---- Your league's scoring weights (pulled from the live league on 2026-10-21) ----
# This is what makes a player "good" in YOUR league specifically. Steals and
# blocks are worth 2x, turnovers hurt, double/triple-doubles get bonuses.
SCORING_WEIGHTS = {
    "pts": 0.5,
    "reb": 1.0,
    "ast": 1.0,
    "stl": 2.0,
    "blk": 2.0,
    "to": -1.0,
    "tpm": 0.5,
    "dd": 1.0,   # double-double bonus
    "td": 2.0,   # triple-double bonus
    "bonus_pt_40p": 2.0,
    "bonus_pt_50p": 2.0,
    "ff": -2.0,  # flagrant foul
    "tf": -2.0,  # technical foul
}


def validate(require_claude: bool = True) -> None:
    """Fail fast with a friendly message if something essential is missing."""
    problems = []
    if require_claude and not ANTHROPIC_API_KEY:
        problems.append("ANTHROPIC_API_KEY is not set")
    if not SLEEPER_LEAGUE_ID:
        problems.append("SLEEPER_LEAGUE_ID is not set")
    if ENABLE_WRITES and not SLEEPER_TOKEN:
        problems.append("ENABLE_WRITES is true but SLEEPER_TOKEN is empty")
    if problems:
        raise SystemExit("Config error:\n  - " + "\n  - ".join(problems))
