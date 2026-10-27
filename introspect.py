"""Score-model diagnostic.

We discovered the bot reads a per-day matchup score (e.g. 91-73) while the app
shows the cumulative week (153.30-224.66). The field we currently sum
(matchup_legs.points) comes back 0 for this league, so we need to find where the
real cumulative lives. This script dumps everything needed to settle that, using
only PUBLIC read endpoints for the sweep and the authenticated GraphQL API
(bot's own token) for the leg-field probe. It prints field names and point
values — never the token.

Run it via the `introspect` workflow command and paste the whole output back.

    python introspect.py
"""

import os
import json
import base64
import requests

LEAGUE_ID = "1411433449088155648"
MY_ROSTER_ID = 14
REST = "https://api.sleeper.app/v1"
GRAPHQL_URL = "https://sleeper.com/graphql"
TOKEN = os.environ.get("SLEEPER_TOKEN")   # only needed for the GraphQL probe


def rest(path):
    r = requests.get(f"{REST}/{path}", timeout=25)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json() if r.text else None


# ====================================================================
# 1) STATE — what does Sleeper think "now" is (week vs leg)?
# ====================================================================
state = rest("state/nba") or {}
week = state.get("week")
leg = state.get("leg")
print("=" * 64)
print("STATE/nba")
print("=" * 64)
print(json.dumps({k: state.get(k) for k in
                  ("season", "season_type", "week", "leg",
                   "display_week", "previous_season")}, indent=2))
print(f"\n-> week={week}  leg={leg}")

# ====================================================================
# 2) PUBLIC REST SWEEP — pull /matchups/{N} for a range of N and show
#    roster 14 + its opponent each time. This reveals whether N indexes
#    a single DAY (points ~91) or the WEEK (points ~153), and whether a
#    subset of N's SUMS to the app's 153.30 / 224.66.
# ====================================================================
print("\n" + "=" * 64)
print("REST /league/.../matchups/{N} SWEEP  (roster 14 = me)")
print("=" * 64)
hi = max((week or 1), (leg or 1))
upper = min(hi + 1, 30)
my_running = 0.0
opp_running = {}
for n in range(1, upper + 1):
    rows = rest(f"league/{LEAGUE_ID}/matchups/{n}")
    if not rows:
        print(f"  N={n:>2}: (empty / 404)")
        continue
    mine = next((m for m in rows if m.get("roster_id") == MY_ROSTER_ID), None)
    if not mine:
        print(f"  N={n:>2}: roster 14 absent ({len(rows)} rows)")
        continue
    mid = mine.get("matchup_id")
    opp = next((m for m in rows if m.get("matchup_id") == mid
                and m.get("roster_id") != MY_ROSTER_ID), None)
    my_pts = float(mine.get("points") or 0)
    opp_pts = float(opp.get("points") or 0) if opp else 0.0
    opp_rid = opp.get("roster_id") if opp else None
    nonzero = sum(1 for v in (mine.get("players_points") or {}).values() if v)
    my_running += my_pts
    if opp_rid is not None:
        opp_running[opp_rid] = opp_running.get(opp_rid, 0.0) + opp_pts
    print(f"  N={n:>2}: me={my_pts:>7.2f}  opp(r{opp_rid})={opp_pts:>7.2f}  "
          f"matchup_id={mid}  my_players_scoring={nonzero}")

print(f"\n  SUM of my points across all N above: {round(my_running, 2)}")
print(f"  SUM of opp points by opponent roster: "
      f"{ {k: round(v, 2) for k, v in opp_running.items()} }")
print("  (If one opponent's sum ~= 224.66 and mine ~= 153.30, the week total "
      "is the SUM of per-day legs.)")

# ====================================================================
# 3) AUTHENTICATED GraphQL — what fields does matchup_legs REALLY have,
#    and are any of them populated with per-leg points for roster 14?
# ====================================================================
print("\n" + "=" * 64)
print("GraphQL matchup_legs FIELD PROBE")
print("=" * 64)
if not TOKEN:
    print("  No SLEEPER_TOKEN in env — skipping (run via the workflow to include it).")
    raise SystemExit(0)


def gql(op, query, variables=None):
    r = requests.post(
        GRAPHQL_URL,
        headers={"authorization": TOKEN, "content-type": "application/json",
                 "accept": "application/json", "origin": "https://sleeper.com",
                 "referer": "https://sleeper.com/", "x-sleeper-graphql-op": op,
                 "user-agent": "sleeper-ai-manager/1.0"},
        json={"operationName": op, "query": query, "variables": variables or {}},
        timeout=30,
    )
    try:
        return r.json()
    except Exception:
        return {"errors": f"HTTP {r.status_code}: {r.text[:160]}"}


def type_fields(type_name):
    q = ("query t($n: String!){ __type(name:$n){ fields{ name "
         "type{ kind name of_type{ kind name } } } } }")
    r = gql("t", q, {"n": type_name})
    t = (r.get("data") or {}).get("__type")
    if not t or not t.get("fields"):
        return [], []
    rendered, scalars = [], []
    for f in t["fields"]:
        tt = f["type"]
        while tt and tt.get("kind") in ("NON_NULL", "LIST"):
            tt = tt.get("of_type")
        kind = (tt or {}).get("kind")
        nm = (tt or {}).get("name")
        rendered.append(f"{f['name']}:{nm or kind}")
        if kind in ("SCALAR", "ENUM"):
            scalars.append(f["name"])
    return rendered, scalars


# discover the return type of matchup_legs via __typename
probe = gql("p", 'query p{ matchup_legs(league_id:"%s", round:%s){ __typename } }'
            % (LEAGUE_ID, week or 1))
if probe.get("errors"):
    print("  matchup_legs probe error:", probe["errors"])
    raise SystemExit(0)
items = (probe.get("data") or {}).get("matchup_legs") or []
tn = items[0]["__typename"] if items else None
print(f"  matchup_legs returned {len(items)} rows, item type = {tn}")
if not tn:
    print("  (no rows for this round)")
    raise SystemExit(0)

rendered, scalars = type_fields(tn)
print(f"  {tn} FIELDS: {', '.join(rendered)}")

# dump roster 14's legs with every scalar field, so we can see which one
# actually carries per-day points (points? starters_points? custom_points?)
sel = " ".join(scalars) if scalars else "__typename"
dump = gql("d", 'query d{ matchup_legs(league_id:"%s", round:%s){ %s } }'
           % (LEAGUE_ID, week or 1, sel))
rows = (dump.get("data") or {}).get("matchup_legs") or []
mine = [r for r in rows if r.get("roster_id") == MY_ROSTER_ID]
print(f"\n  roster 14 legs this round (round={week}):")
print(json.dumps(mine, indent=2)[:2500])

# if there's a per-leg numeric field, sum each candidate across my legs
print("\n  candidate per-leg point sums for roster 14:")
for field in scalars:
    vals = []
    for r in mine:
        v = r.get(field)
        if isinstance(v, (int, float)):
            vals.append(v)
        elif isinstance(v, list):  # e.g. starters_points -> sum the list
            vals.append(sum(x for x in v if isinstance(x, (int, float))))
    if vals:
        print(f"    sum({field}) = {round(sum(vals), 2)}   per-leg: {vals}")
