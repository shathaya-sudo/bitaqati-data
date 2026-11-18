#!/usr/bin/env python3
"""Marks merchant locations whose coordinates are not a real branch position.

`approx: true` is set when the coordinate is Apple Maps' default pin for Doha (25.2854, 51.531 —
what you get for an address without a precise geocode) or when the branch name says "(approx.)".
The app never registers a geofence on an approximate location; it is still used to place the
branch inside a venue (mall) when its coordinate falls within the venue radius.

Also flagged (unless the point lies inside a known venue circle from venues.json next to the seed, where
shared coordinates are the mall itself):
- a coordinate shared by 5 or more different merchants: an area centre (Lusail, Salwa Road, West Bay…), not a shop.
  (Three-decimal coordinates are kept: their error is under ~55 m, within the 150 m branch geofence.)

Usage: annotate_locations.py <seed.json> [venues.json]   (rewrites in place; also importable: annotate(seed, venues))
"""
import json, sys, os, math, collections

DOHA_DEFAULT = (25.2854, 51.531)
TOL = 0.0006


def is_default_pin(lat, lng):
    return abs(lat - DOHA_DEFAULT[0]) < TOL and abs(lng - DOHA_DEFAULT[1]) < TOL


SHARED_BY = 5


def _decimals(v):
    s = repr(float(v))
    return len(s.split(".")[1]) if "." in s and "e" not in s else 0


def _metres(lat1, lng1, lat2, lng2):
    x = math.radians(lng2 - lng1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return 6371000 * math.hypot(x, y)


def _in_venue(lat, lng, venues):
    return any(_metres(lat, lng, v["lat"], v["lng"]) <= v["radius"] for v in venues)


def annotate(seed, venues=None):
    venues = venues or []
    users = collections.defaultdict(set)
    for m in seed["merchants"]:
        for loc in m["locations"]:
            users[(round(loc["lat"], 4), round(loc["lng"], 4))].add(m["id"])
    flagged = 0
    for m in seed["merchants"]:
        for loc in m["locations"]:
            name = (loc.get("nameEn", "") + " " + loc.get("nameAr", "")).lower()
            approx = is_default_pin(loc["lat"], loc["lng"]) or "approx" in name or "تقريب" in name
            if not approx and not _in_venue(loc["lat"], loc["lng"], venues):
                approx = len(users[(round(loc["lat"], 4), round(loc["lng"], 4))]) >= SHARED_BY
            if approx:
                loc["approx"] = True
                flagged += 1
            else:
                loc.pop("approx", None)
    return flagged


def load_venues(seed_path, explicit=None):
    path = explicit or os.path.join(os.path.dirname(os.path.abspath(seed_path)), "venues.json")
    if not os.path.exists(path):
        return []
    return json.load(open(path, encoding="utf-8")).get("venues", [])


if __name__ == "__main__":
    path = sys.argv[1]
    seed = json.load(open(path, encoding="utf-8"))
    n = annotate(seed, load_venues(path, sys.argv[2] if len(sys.argv) > 2 else None))
    json.dump(seed, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    total = sum(len(m["locations"]) for m in seed["merchants"])
    print(f"approx locations: {n} of {total}")
