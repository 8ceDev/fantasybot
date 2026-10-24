"""Discovery — find where PENDING / unaccepted DM invites live.

my_dms only returns DMs we've already accepted, so a first-time trade request
from a new person is invisible there. This script hunts for it two ways:

  1. Introspects the return shapes of get_dm_by_members / requests /
     inbound_requests / outbound_requests (candidate "pending invite" lists).
  2. Empirically probes get_dm_by_members(me, X) for EVERY league member, and
     reports which ones have a DM and whether our membership is still pending.

Since you have a hidden trade request right now, #2 should surface it and tell
us the exact dm_id + pending state — which is everything we need to rebuild the
handler to catch and auto-accept these.

Usage:  python introspect.py
"""

import os
import json
import base64
import requests

TOKEN = os.environ.get("SLEEPER_TOKEN")
LEAGUE_ID = os.environ.get("SLEEPER_LEAGUE_ID")
if not TOKEN or not LEAGUE_ID:
    raise SystemExit("Need SLEEPER_TOKEN and SLEEPER_LEAGUE_ID env vars.")

GRAPHQL_URL = "https://sleeper.com/graphql"
REST = "https://api.sleeper.app/v1"


def decode_jwt(tok):
    body = tok.split(".")[1]
    body += "=" * (-len(body) % 4)
    return json.loads(base64.urlsafe_b64decode(body.encode()))


MY_ID = str(decode_jwt(TOKEN)["user_id"])
print(f"My user_id: {MY_ID}\n")


def gql(op, query, variables):
    r = requests.post(
        GRAPHQL_URL,
        headers={
            "authorization": TOKEN,
            "content-type": "application/json",
            "accept": "application/json",
            "origin": "https://sleeper.com",
            "referer": "https://sleeper.com/",
            "x-sleeper-graphql-op": op,
            "user-agent": "sleeper-ai-manager/1.0",
        },
        json={"operationName": op, "query": query, "variables": variables},
        timeout=30,
    )
    return r.json()


def unwrap_name(t):
    while t:
        if t.get("name"):
            return t["name"]
        t = t.get("of_type")
    return None


def render_type(t):
    if not t:
        return "?"
    k = t.get("kind")
    if k == "NON_NULL":
        return render_type(t.get("of_type")) + "!"
    if k == "LIST":
        return "[" + render_type(t.get("of_type")) + "]"
    return t.get("name") or k or "?"


# ---- 1. Introspect candidate "pending invite" queries ----
INTRO = """
query {
  __schema {
    query_type { fields { name args { name type { kind name of_type { kind name of_type { kind name } } } }
                          type { kind name of_type { kind name of_type { kind name } } } } }
    types { name fields { name type { kind name of_type { kind name of_type { kind name } } } } }
  }
}
"""
intro = gql("x", INTRO, {})
if intro.get("errors"):
    print("introspection errors:", json.dumps(intro["errors"], indent=2))
else:
    schema = intro["data"]["__schema"]
    qf = {f["name"]: f for f in schema["query_type"]["fields"]}
    types_by_name = {t["name"]: t for t in schema["types"] if t.get("name")}
    print("=== Candidate invite/request queries ===\n")
    for name in ["get_dm_by_members", "requests", "inbound_requests", "outbound_requests"]:
        f = qf.get(name)
        if not f:
            print(f"  {name}: NOT FOUND\n")
            continue
        args = ", ".join(f"{a['name']}: {render_type(a['type'])}" for a in f["args"])
        rtn = unwrap_name(f["type"])
        print(f"  {name}({args}) -> {rtn}")
        t = types_by_name.get(rtn)
        if t and t.get("fields"):
            for fld in t["fields"]:
                print(f"       {fld['name']}: {render_type(fld['type'])}")
        print()

# ---- 2. Probe get_dm_by_members for every league member ----
print("=== Per-member DM probe (finds hidden/pending DMs) ===\n")
rosters = requests.get(f"{REST}/league/{LEAGUE_ID}/rosters", timeout=20).json() or []
users = requests.get(f"{REST}/league/{LEAGUE_ID}/users", timeout=20).json() or []
name_by_id = {str(u["user_id"]): u.get("display_name") for u in users}
owner_ids = [str(r["owner_id"]) for r in rosters if r.get("owner_id")]

PROBE = """
query get_dm_by_members($members: [Snowflake]) {
  get_dm_by_members(members: $members) {
    dm_id dm_type last_message_id last_read_id last_author_id last_message_text
  }
}
"""
MEMBERS_Q = """
query dm_members($dm_id: Snowflake!) {
  dm_members(dm_id: $dm_id) { user_id display_name pending }
}
"""

for oid in owner_ids:
    if oid == MY_ID:
        continue
    nm = name_by_id.get(oid, oid)
    try:
        data = gql("get_dm_by_members", PROBE, {"members": [MY_ID, oid]})
        if data.get("errors"):
            print(f"  {nm} ({oid}): error {data['errors'][0].get('message')}")
            continue
        dm = (data.get("data") or {}).get("get_dm_by_members")
        if not dm:
            print(f"  {nm} ({oid}): no DM")
            continue
        dm_id = dm.get("dm_id")
        unread = str(dm.get("last_message_id") or "") != str(dm.get("last_read_id") or "")
        # check my pending status
        pend = None
        md = gql("dm_members", MEMBERS_Q, {"dm_id": str(dm_id)})
        for m in ((md.get("data") or {}).get("dm_members") or []):
            if str(m.get("user_id")) == MY_ID:
                pend = m.get("pending")
        print(f"  {nm}: dm_id={dm_id} pending={pend} unread={unread} "
              f"last=\"{(dm.get('last_message_text') or '')[:45]}\"")
    except Exception as e:
        print(f"  {nm} ({oid}): exception {e}")
