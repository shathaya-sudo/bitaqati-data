#!/usr/bin/env python3
"""Makes every offer's stored value say what its own text says (run by merge_seed.py after merging).

Rules (each one came from a defect class found in the 2026-11-18 data audit):
- A single percentage in the title wins over a different stored value (the title is the source line).
- Two or more percentages in the title ("10–30%", "20% food, 10% drinks") are stored as a range:
  discountValue = low end, discountMax = high end.
- "Up to / حتى / يصل إلى / لغاية" + one percentage → upTo = true (a ceiling, shown as "حتى 30%").
- A valueless offer ("Special offer") whose text carries percentages becomes a percent offer (range if several).
- 100% is never a discount (a free consultation): typed as "other" so it does not top every ranking.
- A Testaahel template title ("خصم 30% لحاملي بطاقة تستاهل في X") whose conditions list several
  different percentages becomes a range with a range title, until the crawler's subtitle titles replace it.
- Scraper artefacts such as "(site line reads 'المعنى')" are removed from titles.
- verified = the source is the issuer itself (its site, portal or app). Press articles and blogs are not.

Usage: fix_offer_values.py <seed.json>   (rewrites in place; importable: fix(seed) -> counts)
"""
import json, re, sys
from urllib.parse import urlparse

PCT = re.compile(r"(\d{1,2}(?:[.,]\d)?)\s*(?:%|٪)")
UPTO = re.compile(r"up\s*to|upto|حتى|يصل(?:ون)? (?:إلى|الى)|تصل (?:إلى|الى)|لغاية|لحد", re.I)
ARTEFACT = re.compile(r"\s*\((?:site line reads|السطر في الموقع)[^)]*\)")
TEMPLATE_AR = re.compile(r"^خصم (\d{1,2}(?:\.\d)?)% لحاملي بطاقة تستاهل في (.+)$")
TEMPLATE_EN = re.compile(r"^(\d{1,2}(?:\.\d)?)% off for Testaahel cardholders at (.+)$")
ISSUER_HOSTS = {"daman.gov.qa", "testaahel.qa", "ooredoo.qa", "vodafone.qa", "qib.com.qa", "hsbc.com.qa",
                "cbq.com.qa", "dukhanbank.com", "qnb.com", "mawared.qa"}
ISSUER_TEXT = re.compile(r"^(Mawared|QIB mobile app|QIB app|Testaahel app|Nojoom app)", re.I)


def pcts(text):
    vals = [float(x.replace(",", ".")) for x in PCT.findall(text or "")]
    return sorted({v for v in vals if 3 <= v < 100})


def fmt(v):
    return str(int(v)) if float(v).is_integer() else str(v)


def is_issuer(source):
    s = (source or "").strip()
    if s.startswith("http"):
        host = urlparse(s).netloc.lower().replace("www.", "")
        return any(host == h or host.endswith("." + h) for h in ISSUER_HOSTS)
    return bool(ISSUER_TEXT.match(s))


def fix(seed):
    c = {"titleWins": 0, "range": 0, "upTo": 0, "fromText": 0, "hundred": 0, "template": 0, "artefact": 0, "unverified": 0}
    for o in seed["offers"]:
        for k in ("titleAr", "titleEn"):
            if o.get(k) and ARTEFACT.search(o[k]):
                o[k] = ARTEFACT.sub("", o[k]).strip(); c["artefact"] += 1
        # The Daman page's broken line "15% المعنى" ("meaning") carries no object: say just the discount.
        if re.fullmatch(r"\d{1,2}% المعنى", o.get("titleAr") or ""):
            o["titleAr"] = "خصم " + o["titleAr"].split()[0]; o["titleEn"] = o["titleAr"].split()[1] + " off"; c["artefact"] += 1
        o.setdefault("discountMax", None); o.setdefault("upTo", False)
        title = (o.get("titleAr") or "") + " | " + (o.get("titleEn") or "")
        cond = (o.get("conditionsAr") or "") + " | " + (o.get("conditionsEn") or "")
        t, dtype, val = pcts(title), o.get("discountType"), o.get("discountValue")

        if dtype == "percent" and val == 100:
            o["discountType"], o["discountValue"], o["discountMax"], o["upTo"] = "other", None, None, False; c["hundred"] += 1
            continue
        if dtype in ("fixed",) and UPTO.search(title) and not o["upTo"]:
            o["upTo"] = True; c["upTo"] += 1
        if dtype not in ("percent", "other"):
            continue
        if dtype == "other" and not t:
            cp = pcts(cond)
            if cp:
                o["discountType"] = "percent"; c["fromText"] += 1
                if len(cp) > 1: o["discountValue"], o["discountMax"] = cp[0], cp[-1]; c["range"] += 1
                else: o["discountValue"], o["upTo"] = cp[0], bool(UPTO.search(cond))
            continue
        if not t:
            continue
        if len(t) > 1:
            if (o["discountValue"], o["discountMax"]) != (t[0], t[-1]):
                o["discountType"], o["discountValue"], o["discountMax"], o["upTo"] = "percent", t[0], t[-1], False; c["range"] += 1
            continue
        n = t[0]
        upto = bool(UPTO.search(title))
        if dtype == "other" or val != n:
            o["discountType"], o["discountValue"], o["discountMax"] = "percent", n, None; c["titleWins"] += 1
        if upto and not o["upTo"]:
            o["upTo"] = True; c["upTo"] += 1
        # Testaahel template hiding several figures in the conditions: show the honest range.
        m_ar, m_en = TEMPLATE_AR.match(o.get("titleAr") or ""), TEMPLATE_EN.match(o.get("titleEn") or "")
        cp = pcts(cond)
        if m_ar and m_en and len(cp) > 1 and not o.get("discountMax"):
            lo, hi = cp[0], cp[-1]
            o["discountValue"], o["discountMax"], o["upTo"] = lo, hi, False
            o["titleAr"] = f"خصم {fmt(lo)}–{fmt(hi)}% لحاملي بطاقة تستاهل في {m_ar.group(2)}"
            o["titleEn"] = f"{fmt(lo)}–{fmt(hi)}% off for Testaahel cardholders at {m_en.group(2)}"
            c["template"] += 1
    for o in seed["offers"]:
        v = is_issuer(o.get("source"))
        if o.get("verified") and not v: c["unverified"] += 1
        o["verified"] = v
    return c


if __name__ == "__main__":
    path = sys.argv[1]
    seed = json.load(open(path, encoding="utf-8"))
    print(fix(seed))
    json.dump(seed, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
