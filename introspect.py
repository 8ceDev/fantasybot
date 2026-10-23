"""Diagnostic — show FULL type signatures for the Sleeper mutations we use.

The first pass told us the mutation NAMES (submit_waiver_claim, accept_trade,
etc.) but collapsed list types, so we couldn't tell if k_adds is `String` or
`[String]`. This version fully unwraps NON_NULL (!) and LIST ([]) wrappers so
we can write the exact payloads with zero guesswork.

Usage:
    python introspect.py
"""

import os
import json
import requests

TOKEN = os.environ.get("SLEEPER_TOKEN")
if not TOKEN:
    raise SystemExit("Set SLEEPER_TOKEN env var first.")

GRAPHQL_URL = "https://sleeper.com/graphql"

# Mutations we actually call (or might switch to). Show their exact signatures.
TARGETS = [
    "submit_waiver_claim",
    "cancel_waiver_claim",
    "update_waiver_claim",
    "league_create_transaction",
    "accept_trade",
    "reject_trade",
    "propose_trade",
    "update_matchup_leg",
    "roster_update_starters",
    "roster_update_reserve",
]

# Unwrap 5 levels deep — more than enough for [String!]! style nesting.
INTROSPECTION = """
query IntrospectionQuery {
  __schema {
    mutation_type {
      fields {
        name
        args {
          name
          type {
            kind name
            of_type {
              kind name
              of_type {
                kind name
                of_type { kind name of_type { kind name } }
              }
            }
          }
        }
      }
    }
  }
}
"""


def render_type(t):
    """Turn a (possibly nested) GraphQL type into a readable string like [String!]!"""
    if not t:
        return "?"
    kind = t.get("kind")
    if kind == "NON_NULL":
        return render_type(t.get("of_type")) + "!"
    if kind == "LIST":
        return "[" + render_type(t.get("of_type")) + "]"
    return t.get("name") or kind or "?"


resp = requests.post(
    GRAPHQL_URL,
    headers={
        "authorization": TOKEN,
        "content-type": "application/json",
        "accept": "application/json",
        "origin": "https://sleeper.com",
        "referer": "https://sleeper.com/",
        "x-sleeper-graphql-op": "IntrospectionQuery",
        "user-agent": "sleeper-ai-manager/1.0",
    },
    json={"operationName": "IntrospectionQuery", "query": INTROSPECTION, "variables": {}},
    timeout=30,
)
data = resp.json()
if "errors" in data and data["errors"]:
    print("GraphQL errors:")
    print(json.dumps(data["errors"], indent=2))
    raise SystemExit(1)

mutations = {m["name"]: m for m in data["data"]["__schema"]["mutation_type"]["fields"]}

print("=== EXACT SIGNATURES for the mutations we use ===\n")
for name in TARGETS:
    m = mutations.get(name)
    if not m:
        print(f"  {name}: NOT FOUND\n")
        continue
    print(f"  {name}(")
    for a in m["args"]:
        print(f"      {a['name']}: {render_type(a['type'])}")
    print("  )\n")
