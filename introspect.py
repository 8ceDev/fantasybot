"""Diagnostic — ask Sleeper's GraphQL API what mutations it actually accepts.

Run this once with your SLEEPER_TOKEN set, and it'll list every mutation
Sleeper knows about, filtered for things related to waivers/adds/drops/trades.
We'll use the output to find the correct mutation name for NBA waiver claims.

Note: Sleeper uses snake_case even for standard GraphQL introspection fields
(mutation_type instead of mutationType, of_type instead of ofType). This
query is written for Sleeper's conventions, not the GraphQL spec.

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

INTROSPECTION = """
query IntrospectionQuery {
  __schema {
    mutation_type {
      fields {
        name
        args {
          name
          type { kind name of_type { name kind } }
        }
      }
    }
  }
}
"""

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

mutations = data["data"]["__schema"]["mutation_type"]["fields"]
print(f"Sleeper has {len(mutations)} mutations total.\n")

# Interesting keywords for us
KEYWORDS = ["waiver", "free_agent", "add", "drop", "trade", "transaction",
            "roster", "lineup", "matchup", "claim"]

interesting = []
for m in mutations:
    name = m["name"]
    if any(kw in name.lower() for kw in KEYWORDS):
        interesting.append(m)

print(f"=== {len(interesting)} relevant mutations ===\n")
for m in interesting:
    args_str = ", ".join(
        f"{a['name']}: {a['type'].get('name') or (a['type'].get('of_type') or {}).get('name') or a['type'].get('kind')}"
        for a in m["args"]
    )
    print(f"  {m['name']}({args_str})")

print("\n=== Full mutation list (grep this for anything we missed) ===")
for m in mutations:
    print(f"  {m['name']}")
