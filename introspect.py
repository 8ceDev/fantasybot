"""Diagnostic — show the RETURN field shapes of the DM/message endpoints.

We know the DM query/mutation names and args now. The last missing piece is
what fields a DM object and a Message object actually expose, so our GraphQL
selections ask for real fields instead of erroring. This resolves the return
type of each key endpoint and prints that type's fields.

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

# Endpoints whose return type we want to see the fields of.
QUERY_TARGETS = ["my_dms", "messages", "dm", "dm_members", "message"]
MUTATION_TARGETS = ["create_message", "create_dm"]

INTROSPECTION = """
query {
  __schema {
    query_type { fields { name type { kind name of_type { kind name of_type { kind name } } } } }
    mutation_type { fields { name type { kind name of_type { kind name of_type { kind name } } } } }
    types {
      kind
      name
      fields {
        name
        type { kind name of_type { kind name of_type { kind name } } }
      }
    }
  }
}
"""


def unwrap_name(t):
    """Drill through LIST/NON_NULL wrappers to the underlying named type."""
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
    json={"operationName": None, "query": INTROSPECTION, "variables": {}},
    timeout=30,
)
data = resp.json()
if "errors" in data and data["errors"]:
    print("GraphQL errors:")
    print(json.dumps(data["errors"], indent=2))
    raise SystemExit(1)

schema = data["data"]["__schema"]
q_fields = {f["name"]: f for f in schema["query_type"]["fields"]}
m_fields = {f["name"]: f for f in schema["mutation_type"]["fields"]}
types_by_name = {t["name"]: t for t in schema["types"] if t.get("name")}

# Resolve each target endpoint -> its return type name
print("=== Endpoint return types ===\n")
wanted_types = set()
for name in QUERY_TARGETS:
    f = q_fields.get(name)
    if f:
        tn = unwrap_name(f["type"])
        wanted_types.add(tn)
        print(f"  query {name} -> {tn}")
for name in MUTATION_TARGETS:
    f = m_fields.get(name)
    if f:
        tn = unwrap_name(f["type"])
        wanted_types.add(tn)
        print(f"  mutation {name} -> {tn}")

# Print the fields of each resolved type
print("\n=== Fields of those types ===")
for tn in sorted(x for x in wanted_types if x):
    t = types_by_name.get(tn)
    if not t:
        print(f"\n  {tn}: (not an object type / no fields)")
        continue
    fields = t.get("fields")
    if not fields:
        print(f"\n  {tn}: (no fields — scalar or enum)")
        continue
    print(f"\n  {tn}:")
    for fld in fields:
        print(f"      {fld['name']}: {render_type(fld['type'])}")
