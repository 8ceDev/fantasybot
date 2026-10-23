"""Sleeper WRITE client — UNOFFICIAL. Read this whole docstring.

Sleeper has NO official write API. Setting a lineup, making a waiver claim, or
responding to a trade all go through Sleeper's private GraphQL endpoint that
the mobile/web app uses. The community has reverse-engineered it (see
cameron-eth/sleeper-sdk). This is undocumented, may break without notice, and
may violate Sleeper's ToS. You said you're fine with that — but we still build
it defensively:

  1. DRY-RUN by default. If ENABLE_WRITES is false or there's no token, every
     "write" just logs what it *would* do and returns. You flip the switch
     only after watching it make sane calls.
  2. Re-read before write. The caller re-fetches the live roster immediately
     before submitting, so if you changed something on your phone we don't
     clobber it blindly.
  3. Never auto-retry a write. A retried roster move can double-submit. On a
     network error we log and bail, we don't retry.
  4. Audit log. Every attempt (intent + outcome) is appended to
     data/audit.log so an interrupted run always leaves a trail.

=============================  IMPORTANT  =============================
The GraphQL mutation strings below are TEMPLATES marked with # VERIFY.
Before you set ENABLE_WRITES=true, confirm the exact operation names and
variable shapes against a current community SDK (cameron-eth/sleeper-sdk)
or by watching the Network tab in your browser while you make one real
move in the Sleeper web app. Swap the verified payloads in here — this is
the ONE file you need to get right. Everything upstream is already correct.
======================================================================
"""

import json
import os
import time
import datetime
import requests

GRAPHQL_URL = "https://sleeper.com/graphql"
AUDIT_LOG = os.path.join(os.path.dirname(__file__), "..", "data", "audit.log")


class DryRun(Exception):
    pass


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
            self._audit(intent, "DRY_RUN", variables)
            return {"dry_run": True}

        headers = {
            "authorization": self.token,
            "content-type": "application/json",
        }
        body = {"operationName": operation_name, "query": query, "variables": variables}
        self._audit(intent, "SUBMITTING", variables)
        try:
            # timeout kept short; we do NOT retry on failure (see docstring #3)
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
    # Each of these is a thin wrapper. The query strings are VERIFY templates.

    def set_lineup(self, roster_id: int, starters: list, reserve: list = None):
        """Set your starting lineup. `starters` is an ordered list of player_ids
        matching the league's roster_positions slots."""
        query = """
        mutation roster_update($leagueId: String!, $rosterId: Int!, $starters: [String!], $reserve: [String!]) {
          roster_update(league_id: $leagueId, roster_id: $rosterId, starters: $starters, reserve: $reserve) {
            roster_id
            starters
          }
        }
        """  # VERIFY operation name + fields
        variables = {
            "leagueId": self.league_id,
            "rosterId": roster_id,
            "starters": starters,
            "reserve": reserve or [],
        }
        return self._graphql("roster_update", query, variables,
                             intent=f"set_lineup roster={roster_id} starters={starters}")

    def waiver_claim(self, roster_id: int, add_player_id: str,
                     drop_player_id: str = None, faab_bid: int = 0):
        """Submit a waiver claim: add a player, optionally drop one, with a FAAB bid."""
        query = """
        mutation waiver_create($leagueId: String!, $rosterId: Int!, $add: String!, $drop: String, $bid: Int) {
          waiver_create(league_id: $leagueId, roster_id: $rosterId, add: $add, drop: $drop, bid: $bid) {
            transaction_id
            status
          }
        }
        """  # VERIFY
        variables = {
            "leagueId": self.league_id,
            "rosterId": roster_id,
            "add": add_player_id,
            "drop": drop_player_id,
            "bid": faab_bid,
        }
        return self._graphql("waiver_create", query, variables,
                             intent=f"waiver add={add_player_id} drop={drop_player_id} bid={faab_bid}")

    def respond_to_trade(self, transaction_id: str, accept: bool):
        """Accept or reject a trade someone proposed to you."""
        op = "trade_accept" if accept else "trade_reject"
        query = f"""
        mutation {op}($transactionId: String!) {{
          {op}(transaction_id: $transactionId) {{
            transaction_id
            status
          }}
        }}
        """  # VERIFY
        return self._graphql(op, query, {"transactionId": transaction_id},
                             intent=f"{op} tx={transaction_id}")

    def propose_trade(self, roster_id: int, send_player_ids: list,
                      receive_player_ids: list, to_roster_id: int):
        """Propose a trade to another roster."""
        query = """
        mutation trade_create($leagueId: String!, $from: Int!, $to: Int!, $send: [String!]!, $receive: [String!]!) {
          trade_create(league_id: $leagueId, from_roster: $from, to_roster: $to, send: $send, receive: $receive) {
            transaction_id
            status
          }
        }
        """  # VERIFY
        variables = {
            "leagueId": self.league_id,
            "from": roster_id,
            "to": to_roster_id,
            "send": send_player_ids,
            "receive": receive_player_ids,
        }
        return self._graphql("trade_create", query, variables,
                             intent=f"propose_trade send={send_player_ids} recv={receive_player_ids}")
