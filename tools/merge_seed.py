#!/usr/bin/env python3 -I
"""Merge partial datasets (seed_parts/*.json) into the app's seed.json.

Usage: python3 -I tools/merge_seed.py <seed.json> <parts_dir> [--drop-offer-ids id1,id2]
- Merchants: new ids are appended; existing ids get new locations appended and known location ids replaced (parts are fresher), plus new aliases.
- Offers: new ids are appended; an existing id is replaced by the part's version (parts are fresher).
- Validates referential integrity, required fields and prints a summary.
- Stamps `generatedAt` with the current UTC time (ISO 8601, e.g. 2026-11-17T18:05:00Z).
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import json, sys, glob, os, re, collections
import datetime as dt

REQ_MERCHANT = ["id", "nameAr", "nameEn", "category", "brandAliases", "locations"]
REQ_OFFER = ["id", "programId", "merchantId", "discountType", "titleAr", "titleEn"]
AR = re.compile(r"[؀-ۿ]")

def main():
    seed_path, parts_dir = sys.argv[1], sys.argv[2]
    drop = set()
    if "--drop-offer-ids" in sys.argv:
        drop = set(sys.argv[sys.argv.index("--drop-offer-ids") + 1].split(","))
    prune_programs = set()
    if "--prune-program" in sys.argv:
        prune_programs = set(sys.argv[sys.argv.index("--prune-program") + 1].split(","))
    part_offer_ids, part_merchant_ids = set(), set()
    seed = json.load(open(seed_path, encoding="utf-8"))
    merchants = {m["id"]: m for m in seed["merchants"]}
    offers = {o["id"]: o for o in seed["offers"]}
    programs = {p["id"] for p in seed["programs"]}
    before = (len(merchants), len(offers))
    problems, warnings = [], []

    # merchant_map.json (next to the parts): reviewed dedupe decisions that must survive every re-crawl.
    #   {"map": {"<part merchant id>": "<canonical merchant id>"}, "dropMerchants": ["<not a merchant>"]}
    merchant_map, drop_merchants = {}, set()
    map_path = os.path.join(parts_dir, "merchant_map.json")
    if os.path.exists(map_path):
        mm = json.load(open(map_path, encoding="utf-8"))
        merchant_map = mm.get("map", {}); drop_merchants = set(mm.get("dropMerchants", []))
        print(f"merchant map: {len(merchant_map)} remaps, {len(drop_merchants)} dropped ids")

    for path in sorted(glob.glob(os.path.join(parts_dir, "*.json"))):
        if os.path.basename(path) == "merchant_map.json":
            continue
        part = json.load(open(path, encoding="utf-8"))
        name = os.path.basename(path)
        # Apply the reviewed merges before anything else: offers follow their merchant to the canonical id.
        part["merchants"] = [m for m in part.get("merchants", []) if m.get("id") not in drop_merchants and m.get("id") not in merchant_map]
        part["offers"] = [o for o in part.get("offers", []) if o.get("merchantId") not in drop_merchants]
        for o in part["offers"]:
            o["merchantId"] = merchant_map.get(o.get("merchantId"), o.get("merchantId"))
        for m in part.get("merchants", []):
            missing = [k for k in REQ_MERCHANT if k not in m]
            if missing:
                problems.append(f"{name}: merchant {m.get('id')} missing {missing}"); continue
            if not m["locations"]:
                problems.append(f"{name}: merchant {m['id']} has no locations"); continue
            if m["id"] in merchants:
                ex = merchants[m["id"]]
                # A part's version of a known location id replaces the seed's (parts are fresher, e.g. geocoded branches).
                fresh = {l["id"]: l for l in m["locations"]}
                kept = [fresh.pop(l["id"], l) for l in ex["locations"]]
                # A re-crawled part carries its old area-level guesses; never add those to a merchant the seed
                # already knows (its locations may have been geocoded or reviewed since). Exact branches are added.
                def is_guess(l):
                    name = (l.get("nameEn", "") + " " + l.get("nameAr", "")).lower()
                    return l.get("approx") or "approx" in name or "تقريب" in name or (
                        abs(l["lat"] - 25.2854) < 0.0006 and abs(l["lng"] - 51.531) < 0.0006)
                ex["locations"] = kept + [l for l in fresh.values() if not is_guess(l)]
                ex["brandAliases"] = list(dict.fromkeys(ex["brandAliases"] + m.get("brandAliases", [])))
            else:
                merchants[m["id"]] = m
        part_merchant_ids |= {m.get("id") for m in part.get("merchants", [])}
        for o in part.get("offers", []):
            missing = [k for k in REQ_OFFER if k not in o]
            if missing:
                problems.append(f"{name}: offer {o.get('id')} missing {missing}"); continue
            part_offer_ids.add(o["id"])
            o.setdefault("conditionsAr", None); o.setdefault("conditionsEn", None)
            o.setdefault("validFrom", None); o.setdefault("validUntil", None)
            o.setdefault("tiers", []); o.setdefault("source", None)
            o.setdefault("verified", False); o.setdefault("lastCheckedAt", None)
            o.setdefault("discountValue", None)
            offers[o["id"]] = o

    for oid in drop:
        offers.pop(oid, None)
    if prune_programs:
        stale = [o["id"] for o in offers.values() if o["programId"] in prune_programs and o["id"] not in part_offer_ids]
        touched = {offers[oid]["merchantId"] for oid in stale}
        for oid in stale:
            offers.pop(oid)
        referenced = {o["merchantId"] for o in offers.values()}
        orphans = [mid for mid in touched if mid in merchants and mid not in referenced and mid not in part_merchant_ids]
        for mid in orphans:
            merchants.pop(mid)
        print(f"pruned {len(stale)} offers of {sorted(prune_programs)} not in any part; removed {len(orphans)} merchants left without offers")

    # integrity
    for o in list(offers.values()):
        if o["programId"] not in programs:
            problems.append(f"offer {o['id']} unknown program {o['programId']}"); offers.pop(o["id"])
        elif o["merchantId"] not in merchants:
            problems.append(f"offer {o['id']} unknown merchant {o['merchantId']}"); offers.pop(o["id"])
    dropped_locations = 0
    for m in list(merchants.values()):
        if not AR.search(m["nameAr"]) and not re.fullmatch(r"[\d\s]+", m["nameAr"]):
            warnings.append(f"merchant {m['id']} nameAr not Arabic: {m['nameAr']}")  # Latin brand names (DFSK, AEG) are allowed
        inside = [l for l in m["locations"] if 24.4 < l["lat"] < 26.3 and 50.7 < l["lng"] < 51.7]
        dropped_locations += len(m["locations"]) - len(inside)
        m["locations"] = inside
        if not inside:
            # Abroad-only partner: not useful for a Qatar map; drop it and its offers.
            merchants.pop(m["id"])
            for o in [o for o in offers.values() if o["merchantId"] == m["id"]]:
                offers.pop(o["id"])
    if dropped_locations:
        print(f"dropped {dropped_locations} locations outside Qatar")

    # Same-merchant locations within ~55 m are one branch: keep the first real one (a stale approximate pin folds into it).
    collapsed = 0
    for m in merchants.values():
        kept = []
        for l in sorted(m["locations"], key=lambda l: bool(l.get("approx"))):
            if any(abs(l["lat"] - k["lat"]) < 0.0005 and abs(l["lng"] - k["lng"]) < 0.0005 for k in kept):
                collapsed += 1; continue
            kept.append(l)
        m["locations"] = [l for l in m["locations"] if any(l is k for k in kept)]
    if collapsed:
        print(f"collapsed {collapsed} duplicate locations")
    seed["merchants"] = sorted(merchants.values(), key=lambda m: m["id"])
    import annotate_locations
    print(f"approx locations flagged: {annotate_locations.annotate(seed)}")
    seed["offers"] = sorted(offers.values(), key=lambda o: o["id"])
    # Full UTC timestamp: the app swaps in a downloaded catalog only when its generatedAt string is
    # greater, so two refreshes on the same day must differ (ISO 8601 strings compare correctly).
    seed["generatedAt"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    json.dump(seed, open(seed_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    per_program = collections.Counter(o["programId"] for o in seed["offers"])
    print(f"merchants {before[0]} -> {len(seed['merchants'])}, offers {before[1]} -> {len(seed['offers'])}")
    print("offers per program:", dict(per_program.most_common()))
    print("verified:", sum(1 for o in seed["offers"] if o.get("verified")))
    if warnings:
        print(f"{len(warnings)} warnings:")
        for w in warnings[:20]: print(" -", w)
    if problems:
        print(f"\n{len(problems)} problems:")
        for p in problems[:40]: print(" -", p)
    return 1 if problems else 0

if __name__ == "__main__":
    sys.exit(main())
