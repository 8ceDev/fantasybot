"""Discovery v3 — locate the pending DM-invite inbox WITHOUT heavy introspection.

Sleeper's full-schema introspection keeps internal-server-erroring. So instead
of introspecting, we probe each candidate query directly using __typename (a
meta-field valid on any object), discover its return type, then read its fields
with a single targeted __type lookup, then actually call it and dump the result.

Goal: find the inbox holding ryansanchez999's unaccepted request + its dm_id.

Usage:  python introspect.py
"""

import os
import json
import base64
import requests

TOKEN = os.environ.get("SLEEPER_TOKEN")
if not TOKEN:
    raise SystemExit("Need SLEEPER_TOKEN env var.")

GRAPHQL_URL = "https://sleeper.com/graphql"
CANDIDATES = ["inbound_requests", "requests", "my_channels", "my_dms"]


def decode_jwt(tok):
    body = tok.split(".")[1]
    body += "=" * (-len(body) % 4)
    return json.loads(base64.urlsafe_b64decode(body.encode()))


MY_ID = str(decode_jwt(TOKEN)["user_id"])
print(f"My user_id: {MY_ID}\n")


def gql(op, query, variables=None):
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
        json={"operationName": op, "query": query, "variables": variables or {}},
        timeout=30,
    )
    try:
        return r.json()
    except Exception:
        return {"errors": {"message": f"HTTP {r.status_code}: {r.text[:120]}"}}


def errmsg(res):
    e = res.get("errors")
    if isinstance(e, dict):
        return e.get("message")
    if isinstance(e, list) and e:
        first = e[0]
        return first.get("message") if isinstance(first, dict) else str(first)
    return None


# arg configurations to try for each candidate (name -> graphql call args, variables)
ARG_TRIES = [
    ("", {}),
    ("(limit: 50)", {}),
    ("(user_id: $uid)", {"uid": MY_ID}),
]


def discover_typename(cand):
    """Find a working arg set + the return typename via __typename."""
    for call_args, variables in ARG_TRIES:
        vdef = "($uid: Snowflake!)" if "$uid" in call_args else ""
        q = f"query probe{vdef} {{ {cand}{call_args} {{ __typename }} }}"
        r = gql("probe", q, variables)
        if r.get("errors"):
            continue
        payload = (r.get("data") or {}).get(cand)
        if payload is None:
            return call_args, variables, None, None  # callable, returns null
        if isinstance(payload, list):
            tn = payload[0]["__typename"] if payload else "EMPTY_LIST"
            return call_args, variables, tn, len(payload)
        if isinstance(payload, dict):
            return call_args, variables, payload.get("__typename"), 1
    return None, None, None, None


def type_fields(type_name):
    q = """query t($n: String!) { __type(name: $n) {
             fields { name type { kind name of_type { kind name of_type { kind name } } } } } }"""
    r = gql("t", q, {"n": type_name})
    t = (r.get("data") or {}).get("__type")
    if not t or not t.get("fields"):
        return [], []
    scalars = []
    rendered = []
    for f in t["fields"]:
        tt = f["type"]
        depth = ""
        while tt and tt.get("kind") in ("NON_NULL", "LIST"):
            tt = tt.get("of_type")
        kind = (tt or {}).get("kind")
        nm = (tt or {}).get("name")
        rendered.append(f"{f['name']}:{nm or kind}")
        if kind in ("SCALAR", "ENUM"):
            scalars.append(f["name"])
    return rendered, scalars


for cand in CANDIDATES:
    print(f"\n{'='*60}\n{cand}\n{'='*60}")
    call_args, variables, typename, count = discover_typename(cand)
    if call_args is None:
        print("  not callable with any tried arg set (needs other required args)")
        continue
    print(f"  callable as: {cand}{call_args}  -> type={typename}  count={count}")
    if not typename or typename in ("EMPTY_LIST",):
        print("  (returned null/empty — nothing in this inbox right now)")
        continue

    rendered, scalars = type_fields(typename)
    if rendered:
        print(f"  {typename} fields: {', '.join(rendered)}")

    sel = " ".join(scalars[:14]) if scalars else "__typename"
    vdef = "($uid: Snowflake!)" if "$uid" in call_args else ""
    q = f"query dump{vdef} {{ {cand}{call_args} {{ {sel} }} }}"
    r = gql("dump", q, variables)
    if r.get("errors"):
        print(f"  dump error: {errmsg(r)}")
    else:
        payload = (r.get("data") or {}).get(cand)
        print(f"  RESULT:\n{json.dumps(payload, indent=2)[:1500]}")
