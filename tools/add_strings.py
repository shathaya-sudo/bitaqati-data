#!/usr/bin/env python3
"""Add ar+en strings to Localizable.xcstrings safely (file-locked, so parallel workers don't clobber each other).

Usage: python3 tools/add_strings.py strings.json
  where strings.json is {"key": ["Arabic", "English"], ...}
"""
import json, sys, fcntl, os
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
path = os.path.join(root, "Bitaqati", "Localizable.xcstrings")
new = json.load(open(sys.argv[1], encoding="utf-8"))
with open(path, "r+", encoding="utf-8") as f:
    fcntl.flock(f, fcntl.LOCK_EX)
    d = json.load(f)
    for k, (ar, en) in new.items():
        d["strings"][k] = {"localizations": {"ar": {"stringUnit": {"state": "translated", "value": ar}},
                                             "en": {"stringUnit": {"state": "translated", "value": en}}}}
    f.seek(0); f.truncate()
    json.dump(d, f, ensure_ascii=False, indent=2)
print(f"added {len(new)} strings; total {len(d['strings'])}")
