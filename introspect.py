"""Diagnostic — introspect Sleeper's DM/message API.

Previous introspections handled roster/waiver/trade mutations. This round
discovers everything related to direct messages: queries for listing DM
channels, reading messages, detecting unread state, and the mutation
signatures for sending messages and accepting DM invites.

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
    query_type {
      fields {
        name
        args {
          name
          type { kind name of_type { kind name of_type { kind name } } }
        }
      }
    }
    mutation_type {
      fields {
        name
        args {
          name
          type { kind name of_type { kind name of_type { kind name } } }
        }
      }
    }
  }
}
"""


def render_type(t):
    if not t:
        return "?"
    k = t.get("kind")
    if k == "NON_NULL":
        return render_type(t.get("of_type")) + "!"
    if k == "LIST":
        return "[" + render_type(t.get("of_type")) + "]"
    return t.get("name") or k or "?"


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

schema = data["data"]["__schema"]
queries = schema["query_type"]["fields"]
mutations = schema["mutation_type"]["fields"]

# What we care about for a DM/chat bot
KEYWORDS = ["dm", "message", "channel", "topic", "thread", "conversation",
            "chat", "unread", "notification", "inbox"]


def is_relevant(name: str) -> bool:
    low = name.lower()
    return any(kw in low for kw in KEYWORDS)


def print_section(title: str, fields: list):
    print(f"\n=== {title} ({len(fields)} total, showing relevant) ===\n")
    rel = [f for f in fields if is_relevant(f["name"])]
    for f in sorted(rel, key=lambda x: x["name"]):
        if not f["args"]:
            print(f"  {f['name']}()")
            continue
        print(f"  {f['name']}(")
        for a in f["args"]:
            print(f"      {a['name']}: {render_type(a['type'])}")
        print("  )")


print_section("QUERIES (reads)", queries)
print_section("MUTATIONS (writes)", mutations)

# Just in case something's named weirdly, print full name list for queries
print(f"\n=== All {len(queries)} query names (grep for anything we missed) ===")
for q in sorted(queries, key=lambda x: x["name"]):
    print(f"  {q['name']}")
