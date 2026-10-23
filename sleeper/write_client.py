"""Sleeper WRITE client — UNOFFICIAL.

Sleeper has NO official write API. Setting a lineup, making a waiver claim,
or responding to a trade all go through Sleeper's private GraphQL endpoint
that the mobile/web app uses. These mutations were verified against the
community SDK at https://github.com/cameron-eth/sleeper-sdk (which
reverse-engineered them from the live app). They are undocumented, may
break without notice, and may violate Sleeper's ToS. You've accepted that
— but we still build defensively:

  1. DRY-RUN by default. If ENABLE_WRITES is false or there's no token,
     every "write" just logs what it *would* do and returns.
  2. Re-read before write. The caller re-fetches the live roster
     immediately before submitting, so a change you made on your phone
     won't get clobbered blindly.
  3. Never auto-retry a write. A retried roster move can double-submit.
     On a network error we log and bail, we don't retry.
  4. Audit log. Every attempt (intent + outcome) is appended to
     data/audit.log so an interrupted run always leaves a trail.
"""

import json
import os
import re
import datetime
import requests

GRAPHQL_URL = "https://sleeper.com/graphql"
AUDIT_LOG = os.path.join(os.path.dirname(__file__), "..", "data", "audit.log")


def _snowflake(value) -> str:
    """Reject anything that is not a numeric Sleeper id before interpolating into GraphQL."""
    s = str(value)
    if not re.fullmatch(r"[0-9]+", s):
        raise ValueError(f"invalid sleeper id: {value!r}")
    return s


def _safe_player_ids(ids):
    """Players ids are alphanumeric (digits for most, DEF codes like DEN for defenses)."""
    out = []
    for pid in ids:
        s = str(pid)
        if not re.fullmatch(r"[A-Za-z0-9]+", s):
            raise ValueError(f"invalid player_id: {pid!r}")
        out.append(s)
    return out


class SleeperWrite:
    def __init__(self, token: str, league_id: str, enable_writes: bool):
        self.token = token
        self.league_id = league_id
        self.enable_writes = enable_writes and bool(token)
        self._session = requests.Session()

    # ---- infrastructure ----
    def _audit(self, intent: str, outcome: str, detail=None):
        line = {
            "ts": datetime.datetime.utcnow().isoformat() + "Z",
            "intent": intent,
            "outcome": outcome,
            "detail": detail,
        }
        os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(line) + "\n")
        print(f"[audit] {intent} -> {outcome}")

    def _graphql(self, operation_name: str, query: str, variables: dict, intent: str):
        """Single choke point for every write. Honors dry-run + audit."""
        if not self.enable_writes:
            self._audit(intent, "DRY_RUN", {"op": operation_name, "vars": variables})
            return {"dry_run": True}

        headers = {
            "authorization": self.token,            # Sleeper uses the raw token, no "Bearer "
            "content-type": "application/json",
            "accept": "application/json",
            "origin": "https://sleeper.com",
            "referer": "https://sleeper.com/",
            "x-sleeper-graphql-op": operation_name, # Sleeper's web app always sends this
            "user-agent": "sleeper-ai-manager/1.0",
        }
        body = {"operationName": operation_name, "query": query, "variables": variables}
        self._audit(intent, "SUBMITTING", {"op": operation_name, "vars": variables})
        try:
            # short timeout; we do NOT retry on failure (see docstring #3)
            r = self._session.post(GRAPHQL_URL, headers=headers, json=body, timeout=15)
        except requests.RequestException as e:
            self._audit(intent, "NETWORK_ERROR_NO_RETRY", str(e))
            raise
        if r.status_code != 200:
            self._audit(intent, f"HTTP_{r.status_code}", r.text[:500])
            raise RuntimeError(f"Sleeper write failed: HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        if data.get("errors"):
            self._audit(intent, "GRAPHQL_ERROR", data["errors"])
            raise RuntimeError(f"Sleeper GraphQL error: {data['errors']}")
        self._audit(intent, "OK", data.get("data"))
        return data

    # ---- the actual moves ----

    def set_lineup(self, roster_id: int, starters: list, leg: int, reserve: list = None):
        """Set the starting lineup for the current matchup leg.

        For NBA fantasy, `leg` is the current NBA week (from state().week). The
        web app inlines league_id/roster_id/leg/round/starters directly into the
        query string — only starters_games is passed as a GraphQL variable (we
        pass {} since we don't need to pin lineups to specific games).

        `reserve` is currently ignored — moving a player to IR is a separate
        mutation (move_to_ir) that isn't wired up yet.
        """
        safe_league = _snowflake(self.league_id)
        safe_roster = int(roster_id)
        safe_leg = int(leg)
        safe_round = int(leg)   # for NBA, round == leg == current week
        safe_starters = json.dumps(_safe_player_ids(starters))

        query = f"""
        mutation update_matchup_leg($starters_games: Map) {{
          update_matchup_leg(
            league_id: "{safe_league}",
            roster_id: {safe_roster},
            leg: {safe_leg},
            round: {safe_round},
            starters: {safe_starters},
            starters_games: $starters_games
          ) {{
            league_id
            leg
            matchup_id
            roster_id
            round
            starters
            players
          }}
        }}
        """
        return self._graphql(
            "update_matchup_leg", query, {"starters_games": {}},
            intent=f"set_lineup roster={roster_id} leg={leg} starters={starters}",
        )

    def waiver_claim(self, roster_id: int, add_player_id: str,
                     drop_player_id: str = None, faab_bid: int = 0):
        """Submit a FAAB waiver claim: add a player, optionally drop one.

        Sleeper's submit_waiver_claim uses parallel key/value LIST args:
          k_adds=[player_id],  v_adds=[roster_id]   -> who gets added where
          k_drops=[player_id], v_drops=[roster_id]  -> who gets dropped
          k_settings=["waiver_bid"], v_settings=[bid]  -> the FAAB bid
        """
        k_adds = [str(add_player_id)] if add_player_id else []
        v_adds = [int(roster_id)] if add_player_id else []
        k_drops = [str(drop_player_id)] if drop_player_id else []
        v_drops = [int(roster_id)] if drop_player_id else []
        k_settings = ["waiver_bid"]
        v_settings = [int(faab_bid)]

        query = """
        mutation submit_waiver_claim(
          $league_id: Snowflake!,
          $k_adds: [String], $v_adds: [Int],
          $k_drops: [String], $v_drops: [Int],
          $k_settings: [String], $v_settings: [Int]
        ) {
          submit_waiver_claim(
            league_id: $league_id,
            k_adds: $k_adds, v_adds: $v_adds,
            k_drops: $k_drops, v_drops: $v_drops,
            k_settings: $k_settings, v_settings: $v_settings
          ) {
            transaction_id status type created
          }
        }
        """
        variables = {
            "league_id": self.league_id,
            "k_adds": k_adds, "v_adds": v_adds,
            "k_drops": k_drops, "v_drops": v_drops,
            "k_settings": k_settings, "v_settings": v_settings,
        }
        return self._graphql(
            "submit_waiver_claim", query, variables,
            intent=f"waiver add={add_player_id} drop={drop_player_id} bid={faab_bid}",
        )

    def add_free_agent(self, roster_id: int, add_player_id: str = None,
                       drop_player_id: str = None):
        """Instant free-agent add/drop (no FAAB bid — for players not on waivers).

        Uses league_create_transaction with type="free_agent" and the same
        parallel key/value LIST args as the waiver claim.
        """
        if not add_player_id and not drop_player_id:
            raise ValueError("Specify at least one of add_player_id or drop_player_id")
        k_adds = [str(add_player_id)] if add_player_id else []
        v_adds = [int(roster_id)] if add_player_id else []
        k_drops = [str(drop_player_id)] if drop_player_id else []
        v_drops = [int(roster_id)] if drop_player_id else []

        query = """
        mutation league_create_transaction(
          $league_id: Snowflake!, $type: String!,
          $k_adds: [String], $v_adds: [Int],
          $k_drops: [String], $v_drops: [Int]
        ) {
          league_create_transaction(
            league_id: $league_id, type: $type,
            k_adds: $k_adds, v_adds: $v_adds,
            k_drops: $k_drops, v_drops: $v_drops
          ) {
            transaction_id status type created
          }
        }
        """
        variables = {
            "league_id": self.league_id,
            "type": "free_agent",
            "k_adds": k_adds, "v_adds": v_adds,
            "k_drops": k_drops, "v_drops": v_drops,
        }
        return self._graphql(
            "league_create_transaction", query, variables,
            intent=f"free_agent add={add_player_id} drop={drop_player_id}",
        )

    def respond_to_trade(self, transaction_id: str, leg: int, accept: bool):
        """Accept or reject a trade someone proposed to you."""
        op = "accept_trade" if accept else "reject_trade"
        query = f"""
        mutation {op}($league_id: Snowflake!, $transaction_id: Snowflake!, $leg: Int!) {{
          {op}(league_id: $league_id, transaction_id: $transaction_id, leg: $leg) {{
            transaction_id status type created
          }}
        }}
        """
        variables = {
            "league_id": self.league_id,
            "transaction_id": str(transaction_id),
            "leg": int(leg),
        }
        return self._graphql(
            op, query, variables,
            intent=f"{op} tx={transaction_id} leg={leg}",
        )

    def propose_trade(self, adds: list, drops: list,
                      draft_picks: list = None, waiver_budget: list = None,
                      expires_at: int = None):
        """Propose a trade.

        adds: list of (player_id, receiving_roster_id) — who GETS each player.
        drops: list of (player_id, sending_roster_id) — who SENDS each player.
        draft_picks: optional, format "owner,season,round,from_roster,to_roster".
        waiver_budget: optional FAAB transfers "from_roster-to_roster-amount".
        """
        k_adds = [str(p) for p, _ in adds]
        v_adds = [int(r) for _, r in adds]
        k_drops = [str(p) for p, _ in drops]
        v_drops = [int(r) for _, r in drops]

        query = """
        mutation propose_trade(
          $league_id: Snowflake!,
          $k_adds: [String], $v_adds: [Int],
          $k_drops: [String], $v_drops: [Int],
          $draft_picks: [String], $waiver_budget: [String],
          $expires_at: Int
        ) {
          propose_trade(
            league_id: $league_id
            k_adds: $k_adds, v_adds: $v_adds
            k_drops: $k_drops, v_drops: $v_drops
            draft_picks: $draft_picks
            waiver_budget: $waiver_budget
            expires_at: $expires_at
          ) {
            transaction_id status type created leg
          }
        }
        """
        variables = {
            "league_id": self.league_id,
            "k_adds": k_adds, "v_adds": v_adds,
            "k_drops": k_drops, "v_drops": v_drops,
            "draft_picks": draft_picks or [],
            "waiver_budget": waiver_budget or [],
            "expires_at": expires_at,
        }
        return self._graphql(
            "propose_trade", query, variables,
            intent=f"propose_trade adds={adds} drops={drops}",
        )
