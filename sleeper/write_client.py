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
import uuid
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

    def set_lineup(self, roster_id: int, starters: list, leg: int,
                   round_: int = None, reserve: list = None):
        """Set the starting lineup for a specific matchup leg.

        NBA uses DAILY lineups: a matchup period (the `round`, = state.week)
        contains one `leg` per scoring day, and each day's leg is edited
        separately. Writing to the wrong leg — e.g. the week number, which is a
        past/locked day — fails with "the move you are trying to make is
        illegal". So `leg` must be the CURRENT day's leg (from matchup_legs /
        state.leg), and `round` the matchup week. For weekly sports (NFL, EPL)
        leg == round == week, which is why passing them equal used to "work"
        everywhere except daily NBA.

        `reserve` is currently ignored — moving a player to IR is a separate
        mutation (move_to_ir) that isn't wired up yet.
        """
        safe_league = _snowflake(self.league_id)
        safe_roster = int(roster_id)
        safe_leg = int(leg)
        safe_round = int(round_ if round_ is not None else leg)
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
            intent=f"set_lineup roster={roster_id} leg={safe_leg} round={safe_round} starters={starters}",
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

    # ======================================================================
    # DIRECT MESSAGES (chat)
    # DM reads go through _graphql_read (always executes — reads never mutate).
    # DM sends go through _graphql (dry-run gated like every other write).
    # ======================================================================

    def _graphql_read(self, operation_name: str, query: str, variables: dict):
        """Execute a GraphQL READ. Always runs (reads are safe) but needs a token."""
        if not self.token:
            raise RuntimeError("DM reads require SLEEPER_TOKEN to be set.")
        headers = {
            "authorization": self.token,
            "content-type": "application/json",
            "accept": "application/json",
            "origin": "https://sleeper.com",
            "referer": "https://sleeper.com/",
            "x-sleeper-graphql-op": operation_name,
            "user-agent": "sleeper-ai-manager/1.0",
        }
        body = {"operationName": operation_name, "query": query, "variables": variables}
        r = self._session.post(GRAPHQL_URL, headers=headers, json=body, timeout=15)
        if r.status_code != 200:
            raise RuntimeError(f"Sleeper read failed: HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        if data.get("errors"):
            raise RuntimeError(f"Sleeper GraphQL read error: {data['errors']}")
        return data.get("data") or {}

    def my_dms(self):
        """List my DM threads (pending ones included — we detect pending per-member)."""
        query = """
        query my_dms($limit: Int) {
          my_dms(limit: $limit) {
            dm_id
            dm_type
            title
            last_message_id
            last_message_text
            last_message_time
            last_author_id
            last_author_display_name
            last_author_is_bot
            last_read_id
          }
        }
        """
        data = self._graphql_read("my_dms", query, {"limit": 50})
        return data.get("my_dms") or []

    def find_dm_with(self, my_user_id: str, other_user_id: str):
        """Return the DM thread between me and one other user, or None.

        Works only for DMs I've already accepted — a still-pending invite
        errors with 'this DM doesn't seem to exist', which we treat as None.
        """
        query = """
        query get_dm_by_members($members: [Snowflake]) {
          get_dm_by_members(members: $members) {
            dm_id dm_type last_message_id last_read_id
            last_author_id last_author_display_name last_message_text
          }
        }
        """
        try:
            data = self._graphql_read(
                "get_dm_by_members", query,
                {"members": [str(my_user_id), str(other_user_id)]},
            )
        except RuntimeError:
            return None
        return data.get("get_dm_by_members")

    def get_matchup_legs(self, round_: int):
        """Read every matchup leg in a round (matchup week).

        For NBA this returns one leg per scoring DAY per roster; each leg object
        carries its own `leg` number, `roster_id`, `round`, and current
        `starters`. This is how we discover the correct leg to write today
        instead of hardcoding leg == week. Best-effort: [] on any failure."""
        query = """
        query matchup_legs($round: Int) {
          matchup_legs(league_id: "%s", round: $round) {
            leg matchup_id roster_id round starters points
          }
        }
        """ % _snowflake(self.league_id)
        try:
            data = self._graphql_read("matchup_legs", query, {"round": int(round_)})
        except RuntimeError:
            return []
        return data.get("matchup_legs") or []

    def get_trades(self, roster_id=None, limit: int = 100):
        """Fetch trades via the authenticated GraphQL endpoint.

        IMPORTANT: the public REST transactions API only returns COMPLETED
        transactions. Open offers (status 'proposed') are only visible here.
        Returns the raw trade dicts (adds/drops/roster_ids/creator/leg/status).
        """
        query = """
        query league_transactions_filtered(
          $league_id: Snowflake!, $type_filters: [String], $status_filters: [String],
          $leg_filters: [Int], $roster_id_filters: [Int], $limit: Int
        ) {
          league_transactions_filtered(
            league_id: $league_id, type_filters: $type_filters,
            status_filters: $status_filters, leg_filters: $leg_filters,
            roster_id_filters: $roster_id_filters, limit: $limit
          ) {
            transaction_id status type creator consenter_ids roster_ids
            created leg adds drops draft_picks waiver_budget
          }
        }
        """
        variables = {
            "league_id": self.league_id,
            "type_filters": ["trade"],
            "status_filters": [],   # empty = all statuses; we filter in code
            "leg_filters": [],
            "roster_id_filters": [int(roster_id)] if roster_id is not None else [],
            "limit": limit,
        }
        data = self._graphql_read("league_transactions_filtered", query, variables)
        return data.get("league_transactions_filtered") or []

    def dm_members(self, dm_id: str):
        """Who's in a DM, including whether MY membership is still pending."""
        query = """
        query dm_members($dm_id: Snowflake!) {
          dm_members(dm_id: $dm_id) {
            user_id
            display_name
            real_name
            is_bot
            is_owner
            pending
          }
        }
        """
        data = self._graphql_read("dm_members", query, {"dm_id": str(dm_id)})
        return data.get("dm_members") or []

    def dm_messages(self, dm_id: str, limit: int = 100):
        """Messages in a DM, oldest-first, capped to the last `limit`. The
        GraphQL query returns the whole thread; we slice in Python, so a high
        limit simply means 'the full conversation' for any normal DM."""
        query = """
        query messages($parent_id: Snowflake!) {
          messages(parent_id: $parent_id) {
            message_id
            author_id
            author_display_name
            author_is_bot
            created
            text_map
            parent_type
          }
        }
        """
        data = self._graphql_read("messages", query, {"parent_id": str(dm_id)})
        msgs = data.get("messages") or []
        msgs.sort(key=lambda m: m.get("created") or 0)
        return msgs[-limit:]

    def send_dm_message(self, dm_id: str, text: str, parent_type: str = "dm"):
        """Post a message into a DM. Replying also auto-accepts a pending DM."""
        query = """
        mutation create_message($text: String, $parent_type: String!,
                                $parent_id: Snowflake!, $client_id: String) {
          create_message(text: $text, parent_type: $parent_type,
                         parent_id: $parent_id, client_id: $client_id) {
            message_id created author_id
          }
        }
        """
        variables = {
            "text": text,
            "parent_type": parent_type,
            "parent_id": str(dm_id),
            "client_id": str(uuid.uuid4()),
        }
        return self._graphql(
            "create_message", query, variables,
            intent=f"dm_reply dm={dm_id} text={text[:60]!r}",
        )

    def clear_unread_dms(self):
        """Mark all DMs read. Best-effort; return type is a scalar so no subselection."""
        query = "mutation clear_unread_dms { clear_unread_dms }"
        return self._graphql("clear_unread_dms", query, {}, intent="clear_unread_dms")
