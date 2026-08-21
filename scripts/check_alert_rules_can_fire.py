#!/usr/bin/env python3
"""Alert-half of the safety-claiming edge manifest.

An alert is a safety-claiming edge: its output is permission ("nothing is wrong").
A rule whose query references a metric with no series CANNOT FIRE, and Prometheus
reports it health=ok because the query parses and evaluates to an empty vector.

Method: Prometheus' own parsed rules + its own metric-name index. No YAML regex.
Label names are stripped first -- they only ever appear inside {...} or by()/without().
"""
import json, re, urllib.request

def get(u):
    return json.load(urllib.request.urlopen(u, timeout=30))

names = set(get("http://localhost:9090/api/v1/label/__name__/values").get("data", []))
assert names, "FAIL LOUDLY: metric-name index empty -- measured nothing, reporting nothing is wrong"

rules = []
for g in get("http://localhost:9090/api/v1/rules").get("data", {}).get("groups", []):
    for r in g.get("rules", []):
        q = r.get("query")
        if q:
            rules.append((g.get("name"), r.get("name"), q))
assert rules, "FAIL LOUDLY: no rules returned"

BRACES = re.compile(r"\{[^}]*\}")            # label matchers
BYWITH = re.compile(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)")
FUNC   = re.compile(r"([A-Za-z_:][A-Za-z0-9_:]*)\s*\(")
IDENT  = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
KW = {"and","or","unless","bool","offset","by","without","on","ignoring",
      "group_left","group_right","start","end","inf","nan"}

missing = []
for grp, alert, q in rules:
    stripped = BYWITH.sub(" ", BRACES.sub(" ", q))
    funcs = set(FUNC.findall(stripped))
    idents = {i for i in IDENT.findall(stripped)} - funcs - KW
    absent = sorted(i for i in idents if i not in names and "_" in i and not i.isdigit())
    if absent:
        missing.append((grp, alert, absent))

print(f"rules_with_a_query                 {len(rules)}")
print(f"metric_names_with_series           {len(names)}")
print(f"rules_that_CANNOT_FIRE             {len(missing)}")
print()
by_metric = {}
for grp, alert, absent in missing:
    for m in absent:
        by_metric.setdefault(m, []).append(alert)
print(f"distinct_absent_metrics            {len(by_metric)}")
print()
for m in sorted(by_metric, key=lambda x: -len(by_metric[x])):
    print(f"  {m}  ({len(by_metric[m])} rule(s))")
    for a in by_metric[m][:3]:
        print(f"      {a}")
