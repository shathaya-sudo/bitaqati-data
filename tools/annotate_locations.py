#!/usr/bin/env python3
"""Marks merchant locations whose coordinates are not a real branch position.

`approx: true` is set when the coordinate is Apple Maps' default pin for Doha (25.2854, 51.531 —
what you get for an address without a precise geocode) or when the branch name says "(approx.)".
The app never registers a geofence on an approximate location; it is still used to place the
branch inside a venue (mall) when its coordinate falls within the venue radius.

Usage: annotate_locations.py <seed.json>   (rewrites in place; also importable: annotate(seed))
"""
import json, sys

DOHA_DEFAULT = (25.2854, 51.531)
TOL = 0.0006


def is_default_pin(lat, lng):
    return abs(lat - DOHA_DEFAULT[0]) < TOL and abs(lng - DOHA_DEFAULT[1]) < TOL


def annotate(seed):
    flagged = 0
    for m in seed["merchants"]:
        for loc in m["locations"]:
            name = (loc.get("nameEn", "") + " " + loc.get("nameAr", "")).lower()
            approx = is_default_pin(loc["lat"], loc["lng"]) or "approx" in name or "تقريب" in name
            if approx:
                loc["approx"] = True
                flagged += 1
            else:
                loc.pop("approx", None)
    return flagged


if __name__ == "__main__":
    path = sys.argv[1]
    seed = json.load(open(path, encoding="utf-8"))
    n = annotate(seed)
    json.dump(seed, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    total = sum(len(m["locations"]) for m in seed["merchants"])
    print(f"approx locations: {n} of {total}")
