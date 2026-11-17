#!/usr/bin/env python3
"""Fails (exit 1) when seed.json would break the app: the same checks SeedDataTests enforces,
runnable without Xcode so the scheduled refresh never publishes a bad catalog.

Usage: check_seed.py <seed.json>
"""
import json, sys, collections

seed = json.load(open(sys.argv[1], encoding="utf-8"))
programs = {p["id"] for p in seed["programs"]}
merchants = {m["id"]: m for m in seed["merchants"]}
problems = []

if len(programs) < 15: problems.append(f"only {len(programs)} programs")
if len(merchants) < 40: problems.append(f"only {len(merchants)} merchants")
if len(seed["offers"]) < 100: problems.append(f"only {len(seed['offers'])} offers")

seen = set()
for o in seed["offers"]:
    if o["programId"] not in programs: problems.append(f"offer {o['id']} unknown program {o['programId']}")
    if o["merchantId"] not in merchants: problems.append(f"offer {o['id']} unknown merchant {o['merchantId']}")
    if not o["id"].startswith(o["programId"] + "-"): problems.append(f"offer {o['id']} id does not start with its program")
    for k in ("verified", "source", "lastCheckedAt", "titleAr", "titleEn"):
        if not o.get(k): problems.append(f"offer {o['id']} missing {k}")
    key = (o["programId"], o["merchantId"], o.get("titleAr"), o.get("discountValue"))
    if key in seen: problems.append(f"duplicate offer {key}")
    seen.add(key)
for m in merchants.values():
    if not m.get("locations"): problems.append(f"merchant {m['id']} has no locations")
    if not m.get("nameAr") or not m.get("nameEn"): problems.append(f"merchant {m['id']} missing a name")

per = collections.Counter(o["programId"] for o in seed["offers"])
print("offers per program:", dict(per.most_common()))
print("generatedAt:", seed.get("generatedAt"))
if problems:
    print(f"{len(problems)} problems:")
    for p in problems[:50]: print(" -", p)
    sys.exit(1)
print("seed OK")
