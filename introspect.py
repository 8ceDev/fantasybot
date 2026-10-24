"""Discovery v2 — locate the PENDING DM-invite inbox.

get_dm_by_members can't see unaccepted DMs (it errors "doesn't exist"), so the
invite from a first-time messager lives in a separate inbox. Likely candidates:
inbound_requests, requests, my_channels, channels_for_user, search_dms.

This probes each one with LIGHT queries (the previous full-schema dump crashed
Sleeper with an internal error). For each candidate it:
  1. introspects just that field's args + return type (targeted, light)
  2. introspects that return type's scalar fields (targeted __type)
  3. actually CALLS it and dumps the raw result, so we can see if the pending
     request from ryansanchez999 shows up and what its dm_id is.

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

CANDIDATES = ["inbound_requests", "requests", "outbound_requests",
              "my_channels", "channels_for_user", "search_dms"]


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
        return {"errors": [{"message": f"HTTP {r.status_code}: {r.text[:120]}"}]}


def unwrap_kind_name(t):
    while t and t.get("kind") in ("NON_NULL", "LIST"):
        t = t.get("of_type")
    return (t or {}).get("name")


def render_type(t):
    if not t:
        return "?"
    k = t.get("kind")
    if k == "NON_NULL":
        return render_type(t.get("of_type")) + "!"
    if k == "LIST":
        return "[" + render_type(t.get("of_type")) + "]"
    return t.get("name") or k or "?"


# Light: just the query_type field defs (no `types` dump — that's what crashed)
Q_FIELDS = """
query {
  __schema {
    query_type {
      fields {
        name
        args { name type { kind name of_type { kind name of_type { kind name } } } }
        type { kind name of_type { kind name of_type { kind name } } }
      }
    }
  }
}
"""
res = gql("qf", Q_FIELDS)
if res.get("errors"):
    print("query_type introspection failed:", res["errors"][:1])
    raise SystemExit(1)
qf = {f["name"]: f for f in res["data"]["__schema"]["query_type"]["fields"]}


def type_scalar_fields(type_name):
    """Return (all_field_render, scalar_field_names) for an object type."""
    q = """
    query t($n: String!) {
      __type(name: $n) {
        name kind
        fields { name type { kind name of_type { kind name of_type { kind name } } } }
      }
    }
    """
    r = gql("t", q, {"n": type_name})
    t = (r.get("data") or {}).get("__type")
    if not t or not t.get("fields"):
        return [], []
    rendered, scalars = [], []
    for f in t["fields"]:
        rendered.append(f"{f['name']}: {render_type(f['type'])}")
        tt = f["type"]
        while tt and tt.get("kind") in ("NON_NULL", "LIST"):
            tt = tt.get("of_type")
        if tt and tt.get("kind") in ("SCALAR", "ENUM"):
            scalars.append(f["name"])
    return rendered, scalars


for name in CANDIDATES:
    print(f"\n{'='*60}\n{name}\n{'='*60}")
    f = qf.get(name)
    if not f:
        print("  NOT FOUND in schema")
        continue
    args_meta = f["args"]
    args_str = ", ".join(f"{a['name']}: {render_type(a['type'])}" for a in args_meta)
    rtn = unwrap_kind_name(f["type"])
    print(f"  signature: {name}({args_str}) -> {rtn}")

    rendered, scalars = type_scalar_fields(rtn) if rtn else ([], [])
    if rendered:
        print(f"  {rtn} fields: {', '.join(rendered)}")

    # Build + run a real call
    var_defs, call_args, variables = [], [], {}
    blocked = None
    for a in args_meta:
        an, at = a["name"], render_type(a["type"])
        if "user_id" in an:
            var_defs.append(f"${an}: {at.rstrip('!')}")
            call_args.append(f"{an}: ${an}")
            variables[an] = MY_ID
        elif an == "limit":
            var_defs.append(f"${an}: {at.rstrip('!')}")
            call_args.append(f"{an}: ${an}")
            variables[an] = 50
        elif an == "term":
            var_defs.append(f"${an}: {at.rstrip('!')}")
            call_args.append(f"{an}: ${an}")
            variables[an] = ""
        elif at.endswith("!"):
            blocked = f"{an}:{at}"
    if blocked:
        print(f"  (skipping call — needs required arg {blocked})")
        continue

    sel = " ".join(scalars[:12]) if scalars else ""
    vd = ("(" + ", ".join(var_defs) + ")") if var_defs else ""
    ca = ("(" + ", ".join(call_args) + ")") if call_args else ""
    sel_block = (" { " + sel + " }") if sel else ""
    call_q = f"query {name}{vd} {{ {name}{ca}{sel_block} }}"
    r = gql(name, call_q, variables)
    if r.get("errors"):
        print(f"  CALL error: {r['errors'][0].get('message')}")
    else:
        payload = (r.get("data") or {}).get(name)
        txt = json.dumps(payload, indent=2)
        print(f"  CALL result: {txt[:1200]}")
