#!/usr/bin/env python3 -I
"""Rebuild the Testaahel seed part (seed_parts/testaahel.json) from the official site.

Usage (from the Bitaqati folder):
    python3 -I tools/refresh_testaahel.py [--out PART] [--cache DIR] [--workers 4] [--delay 0.3]
                                          [--limit N] [--dry-run] [--raw-out FILE]

What it does
  1. Walks https://www.testaahel.qa/offers?page=1.. until a page has no /offer/<id> links.
  2. Fetches every offer page twice: once in English (fresh session) and once in Arabic
     (session that first hit /lang/ar, which stores the locale in the Laravel session cookie;
     the language of every page is verified by its <title> suffix "- Testaahel" / "- تستاهل").
  3. Parses store name, category, discount badge, description, facilities, terms, phone, zone,
     Google Maps link, PDF attachment and image from each page.
  4. Reads /map/markers once: "offer" markers (exact coordinates keyed by offer url) and
     "store" markers (exact coordinates keyed by the Arabic store name, with an address).
  5. Rebuilds the part, keeping the ids of the previous part for every offer (by source url)
     and merchant that still exists, so user data and dedupe decisions survive. New merchants
     get `slug(nameEn)`; new offers `testaahel-<merchant id>` (then -2, -3 …).
     Locations for a NEW merchant: markers → Google Maps link resolved to coordinates →
     an "(approx.)" location at the centroid of the area named in the offer text (same table
     the previous part used) → the Doha default pin "(approx.)". Existing merchants keep
     their previous locations untouched (other tools geocode them).
  6. Offers that vanished from the site are simply not written; run
     `tools/merge_seed.py ... --prune-program testaahel` afterwards so seed.json drops them.
  7. Writes the part (a dated backup `testaahel.json.bak-YYYY-MM-DD-HHMMSS` is kept next to it;
     the suffix is deliberately not .json so merge_seed never picks the backup up) and prints
     an added / removed / changed / unchanged report.

Only the standard library is used (requests / bs4 are not installed on this Mac).
Be polite to the site: ≤4 parallel connections and a small delay between requests.
"""
import argparse
import collections
import concurrent.futures
import datetime as dt
import html
import http.cookiejar
import json
import os
import re
import shutil
import ssl
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://www.testaahel.qa"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
PROGRAM = "testaahel"
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PART = os.path.join(
    os.path.dirname(os.path.dirname(HERE)),
    "research_notes", "تطبيق تجميع خصومات البطاقات", "seed_parts", "testaahel.json")
DEFAULT_SEED = os.path.join(os.path.dirname(HERE), "Bitaqati", "Resources", "seed.json")

TODAY = dt.date.today().isoformat()
DOHA_DEFAULT = (25.2854, 51.531)
QATAR = (24.4, 26.3, 50.7, 51.7)  # lat min/max, lng min/max (same box as merge_seed.py)

# Site category (English name as shown in the breadcrumb / price-text) → merchant category.
SITE_CATEGORY = {
    # dominant mapping of the first (hand-checked) crawl, per site category
    "restaurants and cafes": "restaurant",   # → cafe when the name says cafe/coffee (see map_category)
    "health": "health",                      # → pharmacy when the name says pharmacy
    "fashion": "fashion", "jewelry and watches": "fashion", "jewellery and watches": "fashion",
    "hotels": "hotel", "external offers": "hotel", "britain": "hotel",
    "shops": "other", "home": "other", "occasions": "other", "mobile and apps": "other",
    "special offers": "other", "other": "other", "others": "other", "slider_home_page": "other",
    "beauty centers": "health", "beauty centres": "health", "fitness": "health",
    "cars": "auto", "education": "education", "entertainment": "entertainment",
    "electrical appliances": "electronics", "electronics": "electronics",
    "pharmacies": "pharmacy", "supermarkets": "grocery", "travel": "travel", "tourism": "travel",
}
# Area centroids used by the previous part for "(approx.)" locations (areaEn, areaAr, lat, lng).
# Qatar areas first, then the foreign cities Testaahel's partner hotels are in.
AREAS = [
    ("Lusail", "لوسيل", 25.4059, 51.495), ("Salwa Road", "طريق سلوى", 25.246, 51.467),
    ("Lagoona Mall", "لاجونا مول", 25.37, 51.5287), ("The Pearl", "اللؤلؤة", 25.37, 51.55),
    ("Villaggio", "فيلاجيو", 25.2607, 51.4411), ("Al Wakrah", "الوكرة", 25.1715, 51.6034),
    ("Tawar Mall", "طوار مول", 25.338, 51.4791), ("Katara", "كتارا", 25.359, 51.5265),
    ("C Ring Road", "الدائري الثالث", 25.274, 51.519), ("West Bay", "الخليج الغربي", 25.322, 51.529),
    ("Landmark", "لاندمارك", 25.3245, 51.4517), ("Al Sadd", "السد", 25.283, 51.5),
    ("Msheireb", "مشيرب", 25.2868, 51.5209), ("Doha Festival City", "دوحة فستيفال سيتي", 25.3807, 51.4475),
    ("Al Rayyan", "الريان", 25.2919, 51.424), ("Banana Island", "جزيرة البنانا", 25.363, 51.64),
    ("Umm Salal", "أم صلال", 25.4036, 51.404), ("Al Aziziya", "العزيزية", 25.25, 51.45),
    ("Al Markhiya", "المرخية", 25.338, 51.496), ("Hyatt Plaza", "حياة بلازا", 25.2572, 51.4385),
    ("Mall of Qatar", "مول قطر", 25.3246, 51.3493), ("Al Hazm Mall", "الحزم مول", 25.336, 51.487),
    ("Al Hilal", "الهلال", 25.269, 51.544), ("Old Airport", "المطار القديم", 25.266, 51.552),
    ("Al Khor", "الخور", 25.687, 51.5059), ("B Square Mall", "بي سكوير مول", 25.211, 51.474),
    ("Madinat Khalifa", "مدينة خليفة", 25.33, 51.49), ("Nuaija", "النعيجة", 25.262, 51.526),
    ("City Center", "سيتي سنتر", 25.3246, 51.5303), ("The Gate Mall", "ذا جيت مول", 25.323, 51.5245),
    ("Al Kheesa", "الخيسة", 25.408, 51.448), ("Al Mamoura", "المعمورة", 25.244, 51.51),
    ("Al Mirqab Mall", "المرقاب مول", 25.275, 51.542), ("Al Muntazah", "المنتزه", 25.269, 51.537),
    ("Al Messila", "المسيلة", 25.325, 51.46), ("Gewan Island", "جزيرة جيوان", 25.398, 51.523),
    ("Ezdan Mall Al Gharafa", "إزدان مول الغرافة", 25.336, 51.434), ("Barwa Village", "قرية بروة", 25.21, 51.56),
    ("Barwa City", "مدينة بروة", 25.22, 51.495), ("Umm Ghuwailina", "أم غويلينا", 25.289, 51.546),
    ("Al Gharafa", "الغرافة", 25.336, 51.434), ("Souq Waqif", "سوق واقف", 25.2876, 51.533),
    ("Al Duhail", "الدحيل", 25.351, 51.47), ("Al Thumama", "الثمامة", 25.231, 51.535),
    ("Ain Khaled", "عين خالد", 25.232, 51.472), ("Abu Hamour", "أبو هامور", 25.238, 51.49),
    ("Al Waab", "الوعب", 25.26, 51.45), ("Muaither", "معيذر", 25.27, 51.43),
    ("Al Mansoura", "المنصورة", 25.278, 51.54), ("Bin Mahmoud", "بن محمود", 25.282, 51.52),
    ("Al Wukair", "الوكير", 25.16, 51.55), ("Mesaieed", "مسيعيد", 24.99, 51.55),
    ("Al Shamal", "الشمال", 26.13, 51.2), ("Al Dafna", "الدفنة", 25.32, 51.53),
    ("Place Vendome", "بلاس فاندوم", 25.411, 51.489), ("Vendome Mall", "فاندوم مول", 25.411, 51.489),
    ("Medina Centrale", "مدينة سنترال", 25.3711, 51.5476), ("Porto Arabia", "بورتو أرابيا", 25.368, 51.549),
    ("Al Maha Island", "جزيرة المها", 25.398, 51.527), ("Qetaifan Island", "جزيرة قطيفان", 25.42, 51.53),
    # foreign cities (dropped by merge_seed as outside Qatar, kept here so the part is complete)
    ("Paris", "باريس", 48.8566, 2.3522), ("Versailles", "فرساي", 48.8049, 2.1204),
    ("Paris CDG Airport", "مطار باريس شارل دي غول", 49.0097, 2.5479),
    ("Riyadh", "الرياض", 24.7136, 46.6753), ("Makkah", "مكة المكرمة", 21.4225, 39.8262),
    ("Jeddah", "جدة", 21.4858, 39.1925), ("Al Ahsa", "الأحساء", 25.38, 49.59),
    ("Kuwait City", "مدينة الكويت", 29.3759, 47.9774), ("London", "لندن", 51.5074, -0.1278),
    ("Bangkok", "بانكوك", 13.7563, 100.5018), ("Dubai", "دبي", 25.2048, 55.2708),
    ("Abu Dhabi", "أبوظبي", 24.4539, 54.3773), ("Istanbul", "إسطنبول", 41.0082, 28.9784),
    ("Manama", "المنامة", 26.2285, 50.5861), ("Muscat", "مسقط", 23.588, 58.3829),
    ("Cairo", "القاهرة", 30.0444, 31.2357), ("Beirut", "بيروت", 33.8938, 35.5018),
    ("Amman", "عمّان", 31.9454, 35.9284), ("Madinah", "المدينة المنورة", 24.5247, 39.5692),
    ("Geneva", "جنيف", 46.2044, 6.1432), ("Milan", "ميلانو", 45.4642, 9.19),
    ("Rome", "روما", 41.9028, 12.4964), ("Madrid", "مدريد", 40.4168, -3.7038),
    ("Barcelona", "برشلونة", 41.3874, 2.1686), ("Vienna", "فيينا", 48.2082, 16.3738),
    ("Munich", "ميونخ", 48.1351, 11.582), ("Zurich", "زيورخ", 47.3769, 8.5417),
    ("Kuala Lumpur", "كوالالمبور", 3.139, 101.6869), ("Maldives", "المالديف", 4.1755, 73.5093),
    ("New York", "نيويورك", 40.7128, -74.006), ("Sharm El Sheikh", "شرم الشيخ", 27.9158, 34.33),
    ("Tunis", "تونس", 36.8065, 10.1815), ("Casablanca", "الدار البيضاء", 33.5731, -7.5898),
    ("Marrakech", "مراكش", 31.6295, -7.9811),
]
# Area synonyms found in offer texts (lower-cased English or normalized Arabic → areaEn in AREAS).
AREA_ALIASES = {
    "pearl qatar": "The Pearl", "the pearl-qatar": "The Pearl", "pearl": "The Pearl", "اللؤلؤة": "The Pearl",
    "lusail": "Lusail", "لوسيل": "Lusail", "west bay": "West Bay", "westbay": "West Bay", "الخليج الغربي": "West Bay",
    "al wakra": "Al Wakrah", "wakrah": "Al Wakrah", "wakra": "Al Wakrah", "الوكرة": "Al Wakrah", "الوكره": "Al Wakrah",
    "al khor": "Al Khor", "الخور": "Al Khor", "katara": "Katara", "كتارا": "Katara",
    "villaggio": "Villaggio", "فيلاجيو": "Villaggio", "فيلاغيو": "Villaggio",
    "landmark mall": "Landmark", "landmark": "Landmark", "لاندمارك": "Landmark",
    "mall of qatar": "Mall of Qatar", "مول قطر": "Mall of Qatar",
    "doha festival city": "Doha Festival City", "festival city": "Doha Festival City", "فستيفال سيتي": "Doha Festival City",
    "tawar mall": "Tawar Mall", "طوار مول": "Tawar Mall", "lagoona": "Lagoona Mall", "لاجونا": "Lagoona Mall",
    "hyatt plaza": "Hyatt Plaza", "حياة بلازا": "Hyatt Plaza", "al hazm": "Al Hazm Mall", "الحزم": "Al Hazm Mall",
    "msheireb": "Msheireb", "mushaireb": "Msheireb", "مشيرب": "Msheireb", "al sadd": "Al Sadd", "السد": "Al Sadd",
    "salwa road": "Salwa Road", "طريق سلوى": "Salwa Road", "c ring road": "C Ring Road", "c-ring road": "C Ring Road",
    "الدائري الثالث": "C Ring Road", "al rayyan": "Al Rayyan", "الريان": "Al Rayyan",
    "umm salal": "Umm Salal", "أم صلال": "Umm Salal", "ام صلال": "Umm Salal", "al aziziya": "Al Aziziya", "aziziyah": "Al Aziziya",
    "العزيزية": "Al Aziziya", "al markhiya": "Al Markhiya", "markhiya": "Al Markhiya", "المرخية": "Al Markhiya",
    "al hilal": "Al Hilal", "الهلال": "Al Hilal", "old airport": "Old Airport", "المطار القديم": "Old Airport",
    "b square": "B Square Mall", "madinat khalifa": "Madinat Khalifa", "مدينة خليفة": "Madinat Khalifa",
    "nuaija": "Nuaija", "النعيجة": "Nuaija", "city center": "City Center", "city centre": "City Center",
    "سيتي سنتر": "City Center", "gate mall": "The Gate Mall", "ذا جيت مول": "The Gate Mall",
    "al kheesa": "Al Kheesa", "الخيسة": "Al Kheesa", "al mamoura": "Al Mamoura", "المعمورة": "Al Mamoura",
    "mirqab mall": "Al Mirqab Mall", "المرقاب مول": "Al Mirqab Mall", "al muntazah": "Al Muntazah", "المنتزه": "Al Muntazah",
    "al messila": "Al Messila", "المسيلة": "Al Messila", "gewan": "Gewan Island", "جيوان": "Gewan Island",
    "ezdan mall": "Ezdan Mall Al Gharafa", "barwa village": "Barwa Village", "قرية بروة": "Barwa Village",
    "barwa city": "Barwa City", "مدينة بروة": "Barwa City", "umm ghuwailina": "Umm Ghuwailina", "أم غويلينا": "Umm Ghuwailina",
    "al gharafa": "Al Gharafa", "gharafa": "Al Gharafa", "الغرافة": "Al Gharafa", "souq waqif": "Souq Waqif", "سوق واقف": "Souq Waqif",
    "al duhail": "Al Duhail", "duhail": "Al Duhail", "الدحيل": "Al Duhail", "al thumama": "Al Thumama", "الثمامة": "Al Thumama",
    "ain khaled": "Ain Khaled", "عين خالد": "Ain Khaled", "abu hamour": "Abu Hamour", "أبو هامور": "Abu Hamour",
    "al waab": "Al Waab", "الوعب": "Al Waab", "muaither": "Muaither", "معيذر": "Muaither",
    "al mansoura": "Al Mansoura", "المنصورة": "Al Mansoura", "bin mahmoud": "Bin Mahmoud", "بن محمود": "Bin Mahmoud",
    "al wukair": "Al Wukair", "الوكير": "Al Wukair", "mesaieed": "Mesaieed", "مسيعيد": "Mesaieed",
    "al dafna": "Al Dafna", "الدفنة": "Al Dafna", "place vendome": "Place Vendome", "vendome": "Vendome Mall",
    "فاندوم": "Place Vendome", "medina centrale": "Medina Centrale", "porto arabia": "Porto Arabia", "بورتو أرابيا": "Porto Arabia",
    "al maha island": "Al Maha Island", "جزيرة المها": "Al Maha Island", "qetaifan": "Qetaifan Island", "قطيفان": "Qetaifan Island",
    "banana island": "Banana Island", "جزيرة البنانا": "Banana Island",
    "paris": "Paris", "باريس": "Paris", "versailles": "Versailles", "فرساي": "Versailles",
    "charles de gaulle": "Paris CDG Airport", "cdg": "Paris CDG Airport", "roissy": "Paris CDG Airport",
    "riyadh": "Riyadh", "الرياض": "Riyadh", "makkah": "Makkah", "mecca": "Makkah", "مكة": "Makkah", "مكه": "Makkah",
    "jeddah": "Jeddah", "جدة": "Jeddah", "al ahsa": "Al Ahsa", "الأحساء": "Al Ahsa", "الاحساء": "Al Ahsa",
    "kuwait": "Kuwait City", "الكويت": "Kuwait City", "london": "London", "لندن": "London", "bangkok": "Bangkok", "بانكوك": "Bangkok",
    "dubai": "Dubai", "دبي": "Dubai", "abu dhabi": "Abu Dhabi", "أبوظبي": "Abu Dhabi", "ابوظبي": "Abu Dhabi",
    "istanbul": "Istanbul", "إسطنبول": "Istanbul", "اسطنبول": "Istanbul", "manama": "Manama", "bahrain": "Manama", "البحرين": "Manama",
    "muscat": "Muscat", "مسقط": "Muscat", "cairo": "Cairo", "القاهرة": "Cairo", "beirut": "Beirut", "بيروت": "Beirut",
    "amman": "Amman", "عمان": "Amman", "madinah": "Madinah", "medina": "Madinah", "المدينة المنورة": "Madinah",
    "geneva": "Geneva", "جنيف": "Geneva", "milan": "Milan", "ميلانو": "Milan", "rome": "Rome", "روما": "Rome",
    "madrid": "Madrid", "مدريد": "Madrid", "barcelona": "Barcelona", "برشلونة": "Barcelona", "vienna": "Vienna", "فيينا": "Vienna",
    "munich": "Munich", "ميونخ": "Munich", "zurich": "Zurich", "زيورخ": "Zurich", "kuala lumpur": "Kuala Lumpur", "كوالالمبور": "Kuala Lumpur",
    "maldives": "Maldives", "المالديف": "Maldives", "new york": "New York", "نيويورك": "New York",
    "sharm el sheikh": "Sharm El Sheikh", "شرم الشيخ": "Sharm El Sheikh", "tunis": "Tunis", "تونس": "Tunis",
    "casablanca": "Casablanca", "الدار البيضاء": "Casablanca", "marrakech": "Marrakech", "مراكش": "Marrakech",
}
AREA_BY_EN = {a[0]: a for a in AREAS}
FOREIGN = {a[0] for a in AREAS if not (QATAR[0] < a[2] < QATAR[1] and QATAR[2] < a[3] < QATAR[3])}

SUFFIX_AR = "لحاملي بطاقة تستاهل؛ تُعرض البطاقة قبل الدفع."
SUFFIX_EN = "For Testaahel cardholders; show the card before paying."
PDF_AR = "التفاصيل الكاملة في ملف PDF المرفق على موقع تستاهل."
PDF_EN = "Full details in the PDF attached on testaahel.qa."

AR_LETTERS = re.compile(r"[؀-ۿ]")
TAG = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")


# --------------------------------------------------------------------------- HTTP

class Site:
    """Two polite sessions (English and Arabic), optional on-disk cache."""

    def __init__(self, cache_dir=None, delay=0.3, workers=4):
        self.cache_dir = cache_dir
        self.delay = delay
        self.workers = workers
        self._lock = threading.Lock()
        self._last = 0.0
        self.en = self._opener()
        self.ar = self._opener()
        self.requests = 0

    @staticmethod
    def _ssl_context():
        """python.org builds ship an empty cert directory; fall back to the system bundle."""
        ctx = ssl.create_default_context()
        if not ctx.get_ca_certs():
            for cafile in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
                if os.path.exists(cafile):
                    ctx.load_verify_locations(cafile)
                    break
        return ctx

    @staticmethod
    def _opener():
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=Site._ssl_context()),
                                         urllib.request.HTTPCookieProcessor(jar))
        op.addheaders = [("User-Agent", UA), ("Accept", "text/html,application/json;q=0.9,*/*;q=0.8"),
                         ("Accept-Language", "en,ar;q=0.8")]
        return op

    def _throttle(self):
        with self._lock:
            wait = self._last + self.delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.requests += 1

    def _cache_path(self, key):
        if not self.cache_dir:
            return None
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", key)
        return os.path.join(self.cache_dir, safe)

    def get(self, url, lang="en", key=None, retries=3):
        path = self._cache_path(key or f"{lang}_{url}")
        if path and os.path.exists(path):
            return open(path, encoding="utf-8").read()
        opener = self.ar if lang == "ar" else self.en
        err = None
        for attempt in range(retries):
            self._throttle()
            try:
                with opener.open(url, timeout=40) as r:
                    text = r.read().decode("utf-8", "replace")
                break
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                err = e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                err = e
            time.sleep(1.5 * (attempt + 1))
        else:
            raise RuntimeError(f"GET {url} failed: {err}")
        if path:
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        return text

    def switch_arabic(self):
        """Hit /lang/ar with the Arabic session (sets the locale in its session cookie)."""
        self._throttle()
        with self.ar.open(BASE + "/lang/ar", timeout=40) as r:
            r.read()

    def resolve_redirect(self, url):
        """Return the Location header of a short link (Google Maps) without following it."""
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=self._ssl_context()), NoRedirect)
        op.addheaders = [("User-Agent", UA)]
        self._throttle()
        try:
            with op.open(url, timeout=30) as r:
                return r.geturl()
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                return e.headers.get("Location")
            return None
        except (urllib.error.URLError, TimeoutError, OSError):
            return None


# --------------------------------------------------------------------------- parsing helpers

def clean(s):
    """Un-escape entities, drop tags, collapse whitespace."""
    if s is None:
        return ""
    s = TAG.sub(" ", s)
    s = html.unescape(html.unescape(s))  # listing titles are escaped twice (&amp;amp;)
    s = s.replace("\xa0", " ")
    return WS.sub(" ", s).strip()


def text_block(s):
    """Like clean() but keeps line breaks (for descriptions / terms), joining lines with ' '."""
    if s is None:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(p|div|li|h\d)>", "\n", s, flags=re.I)
    s = TAG.sub(" ", s)
    s = html.unescape(s).replace("\xa0", " ")
    lines = [WS.sub(" ", ln).strip(" \t•-–*·") for ln in s.split("\n")]
    lines = [ln for ln in lines if ln]
    out = []
    for ln in lines:
        if ln and ln[-1] not in ".。!?؟:؛;":
            ln += "."
        out.append(ln)
    return " ".join(out)


def between(s, start_pat, end_pat, flags=re.S):
    m = re.search(start_pat + r"(.*?)" + end_pat, s, flags)
    return m.group(1) if m else None


def parse_offer_page(html_text, offer_id):
    """Extract the fields of one offer page (works for both languages)."""
    if not html_text:
        return None
    main = html_text[html_text.find("<main"):] if "<main" in html_text else html_text
    main = re.sub(r"<script.*?</script>", "", main, flags=re.S)
    cut = main.find('class="related-section"')
    if cut > 0:
        main = main[:cut]
    d = {"id": offer_id}
    title_tag = clean(between(html_text, r"<title>", r"</title>") or "")
    d["pageTitle"] = title_tag
    d["lang"] = "ar" if title_tag.endswith("تستاهل") else ("en" if title_tag.lower().endswith("testaahel") else "?")
    d["name"] = clean(between(main, r'<h1 class="product-title">', r"</h1>"))
    d["subtitle"] = clean(between(main, r'<p class="product-subtitle">', r"</p>"))
    d["badge"] = clean(between(main, r'<div class="discount-badge-large">', r"</div>"))
    d["category"] = clean(between(main, r'<span class="price-text">', r"</span>"))
    m = re.search(r'offers\?categories=(\d+)"[^>]*>\s*([^<]+?)\s*<', main)
    d["categoryId"] = int(m.group(1)) if m else None
    if m and not d["category"]:
        d["category"] = clean(m.group(2))
    d["image"] = (re.search(r'<img src="([^"]+)"[^>]*class="main-image"', main) or [None, None])[1]
    # highlights: <h4>Label</h4><p ...>value</p>
    d["highlights"] = {}
    for h4, p in re.findall(r"<h4>\s*([^<]+?)\s*</h4>\s*<p[^>]*>(.*?)</p>", main, flags=re.S):
        d["highlights"][clean(h4)] = clean(p)
    hl = d["highlights"]
    d["phone"] = hl.get("Phone") or hl.get("الهاتف") or ""
    tel = re.search(r'href="tel:([^"]+)"', main)
    if tel and not d["phone"]:
        d["phone"] = clean(tel.group(1))
    zone = (hl.get("Zone") or hl.get("المنطقة") or "").strip()
    d["zone"] = zone if re.fullmatch(r"[1-9]\d?", zone) else ""  # Qatar zones are 1–98; the field is often misused
    maps = re.search(r'<a href="(https?://(?:maps\.app\.goo\.gl|goo\.gl/maps|www\.google\.[a-z.]+/maps|maps\.google\.[a-z.]+|g\.page)[^"]*)"', main)
    d["mapsUrl"] = html.unescape(maps.group(1)) if maps else None
    d["pdf"] = (re.search(r'href="([^"]+\.pdf)"', main, flags=re.I) or [None, None])[1]
    d["video"] = (re.search(r'<source src="([^"]+\.mp4)"', main, flags=re.I) or [None, None])[1]
    short = between(main, r'<div class="short-description">', r"</div>")
    full = between(main, r'<div class="full-description">', r"</div>")
    if short is None and full is None:
        desc_tab = between(main, r'<div class="tab-content active" id="description">', r'<div class="tab-content" id=|</div>\s*</div>\s*</div>')
        full = desc_tab
    d["shortDescription"] = text_block(short)
    d["description"] = text_block(full)
    d["facilities"] = text_block(between(main, r'<div class="tab-content" id="facilities">\s*<div[^>]*>', r"</div>"))
    d["terms"] = text_block(between(main, r'<div class="tab-content" id="terms">\s*<div[^>]*>', r"</div>"))
    return d


def parse_listing(html_text):
    """Offer ids on one listing page, in page order, with badge / category / image."""
    out = []
    for card in re.findall(r'<div class="offer-card">(.*?)</a>\s*</div>', html_text, flags=re.S):
        m = re.search(r'/offer/(\d+)"', card)
        if not m:
            continue
        out.append({
            "id": int(m.group(1)),
            "badge": clean(between(card, r'<div class="offer-badge">', r"</div>")),
            "category": clean(between(card, r'<div class="offer-category-badge">', r"</div>")),
            "title": clean(between(card, r'<h3 class="offer-title">', r"</h3>")),
            "image": (re.search(r'<img src="([^"]+)"', card) or [None, None])[1],
        })
    total = re.search(r"of\s*<strong>(\d+)</strong>\s*results", html_text)
    return out, (int(total.group(1)) if total else None)


# --------------------------------------------------------------------------- normalisation

def slug(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().replace("&", " and ").replace("'", "").replace("’", "")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "store"


def norm_ar(s):
    s = unicodedata.normalize("NFKC", s or "")
    s = re.sub(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]", "", s)
    for a, b in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ة", "ه"), ("ى", "ي"), ("ؤ", "و"), ("ئ", "ي"), ("ء", ""),
                 ("ڤ", "ف"), ("گ", "ك"), ("ک", "ك"), ("ی", "ي"), ("چ", "ج"), ("پ", "ب")):
        s = s.replace(a, b)
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    return WS.sub(" ", s).strip()


def norm_key(s):
    """Language-agnostic key for exact store-name matching (markers ↔ offers)."""
    return norm_ar(s).replace(" ", "")


UPTO = r"(?:up\s*to|upto|حتى|يصل\s*(?:إلى|الى)?|لغاية|خصم\s*حتى)"
NUM = r"(\d{1,3}(?:[.,]\d+)?)"
PCT = r"\s*(?:%|٪|percent|بالمئة|بالمائة)"
TAIL = r"\s*(?:off|discount|خصم|on\s+all|على\s+الكل)?\s*[.!]?"
BOGO = re.compile(r"buy\s*(?:1|one)\s*(?:,|and|&)?\s*get\s*(?:1|one)|\b1\s*\+\s*1\b|\bbogo\b|اشتر[يِ]?\s*(?:واحد[ةه]?|قطعة|1)\s*و?\s*(?:احصل|خذ)", re.I)


def parse_badge(badge):
    """Tight patterns only – anything else is decided from the texts (percent_from_text) or stays 'other'.
    '20%' / 'خصم 20٪' → percent exact; 'Up to 25%' → percent up-to; '10-20%' / '10% to 20%' → range;
    'Up to 6,000 QAR Discount' → fixed; 'BUY 1 GET 1 FREE' → bogo."""
    b = (badge or "").strip().lower()
    if BOGO.search(b):
        return "bogo", None, "exact", None
    lead = r"^\s*(?:خصم|discount|save|enjoy)?\s*"
    sep = r"\s*(?:[-–]|to|إلى|الى|حتى)\s*"
    m = re.fullmatch(lead + NUM + PCT + "?" + sep + NUM + PCT + TAIL + "$", b)
    if m:
        lo, hi = sorted((float(m.group(1).replace(",", ".")), float(m.group(2).replace(",", "."))))
        return "percent", lo, "range", hi
    m = re.fullmatch(lead + "(" + UPTO + r")?\s*" + NUM + PCT + TAIL + "$", b)
    if m:
        return "percent", float(m.group(2).replace(",", ".")), ("upto" if m.group(1) else "exact"), None
    m = re.fullmatch(lead + "(" + UPTO + r")?\s*(\d[\d,]*(?:\.\d+)?)\s*(?:qar|qr|ر\.?\s?ق|ريال)" + TAIL + "$", b)
    if m:
        return "fixed", float(m.group(2).replace(",", "")), ("upto" if m.group(1) else "exact"), None
    return "other", 0, "special", None


GENERIC_BADGES = {"special offer", "special offers", "exclusive offer", "exclusive offers", "عرض خاص", "عروض خاصة", ""}
PCT_IN_TEXT = re.compile(r"(\d{1,2}(?:\.\d)?)\s*(?:%|٪|بالمئة|بالمائة|percent\b)", re.I)


def percent_from_text(texts):
    """Percentages written in the offer texts (badge, subtitle, descriptions – in that order).
    Returns (kind, value, None): the FIRST figure mentioned is the merchant's headline figure (the
    first crawl chose the same one in 131 of 133 multi-figure offers); figures below 3 % are ignored
    (insurance rates such as "1.25 % of the vehicle value" are not discounts)."""
    vals = []
    for t in texts:
        vals += [float(x) for x in PCT_IN_TEXT.findall(t or "")]
    vals = [v for v in vals if 3 <= v < 100]
    if not vals:
        return None
    if len(set(vals)) == 1:
        return "exact", vals[0], None
    # Several figures ("20% on food, 10% on drinks"): an honest range, low end first.
    return "range", min(vals), max(vals)


def fmt_num(v):
    return str(int(v)) if float(v).is_integer() else str(v)


JUNK_ALIAS = re.compile(r"\d|%|٪|discount|offer|rate|special|exclusive|free|خصم|عرض|حصري|مجان", re.I)


def is_brand_alias(alias, merchant):
    """A page/listing title is worth keeping as a search alias only when it is a name, not a slogan."""
    alias = (alias or "").strip()
    if not alias or JUNK_ALIAS.search(alias) or len(alias) > 60:
        return False
    known = {merchant["nameEn"], merchant["nameAr"], *merchant["brandAliases"]}
    return alias not in known and norm_key(alias) not in {norm_key(k) for k in known}


def map_category(site_cat, name_en="", name_ar="", old_cat=None):
    key = WS.sub(" ", (site_cat or "").strip().lower().replace("&", "and"))
    if key not in SITE_CATEGORY:
        return (old_cat or "other"), False
    cat = SITE_CATEGORY[key]
    names = (name_en or "").lower() + " " + norm_ar(name_ar or "")
    if cat == "restaurant" and re.search(r"\b(cafe|caffe|coffee|roaster|bakery|patisserie|dessert|sweets?|chocolate)\b|كافيه|مقهى|قهوه|حلويات|شوكولا|مخبز", names):
        cat = "cafe"
    if cat == "health" and re.search(r"\bpharmac|صيدلي", names):
        cat = "pharmacy"
    return cat, True


# --------------------------------------------------------------------------- locations

def coords_from_maps_url(url):
    if not url:
        return None
    u = urllib.parse.unquote(url)
    for pat in (r"[?&]q=(-?\d+\.\d+),\s*(-?\d+\.\d+)", r"@(-?\d+\.\d+),(-?\d+\.\d+)",
                r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", r"[?&]ll=(-?\d+\.\d+),(-?\d+\.\d+)",
                r"[?&]query=(-?\d+\.\d+),\s*(-?\d+\.\d+)", r"[?&]destination=(-?\d+\.\d+),\s*(-?\d+\.\d+)"):
        m = re.search(pat, u)
        if m:
            lat, lng = float(m.group(1)), float(m.group(2))
            if -90 <= lat <= 90 and -180 <= lng <= 180:
                return round(lat, 6), round(lng, 6)
    return None


def in_qatar(lat, lng):
    return QATAR[0] < lat < QATAR[1] and QATAR[2] < lng < QATAR[3]


def nearest_area(lat, lng, max_km=3.0):
    best, best_d = None, None
    for a in AREAS:
        if a[0] in FOREIGN:
            continue
        d = ((lat - a[2]) * 111.0) ** 2 + ((lng - a[3]) * 100.6) ** 2
        if best_d is None or d < best_d:
            best, best_d = a, d
    if best and best_d is not None and best_d ** 0.5 <= max_km:
        return best
    return None


def area_in_text(texts):
    """First area whose alias appears in the texts (longest alias wins)."""
    joined_en = " ".join(t for t in texts if t).lower()
    joined_ar = norm_ar(" ".join(t for t in texts if t))
    hits = []
    for alias, area_en in AREA_ALIASES.items():
        if AR_LETTERS.search(alias):
            hay, needle = joined_ar, norm_ar(alias)
        else:
            hay, needle = joined_en, alias
        if needle and re.search(r"(?<![\w])" + re.escape(needle) + r"(?![\w])", hay):
            hits.append((len(needle), area_en))
    if not hits:
        return None
    hits.sort(reverse=True)
    return AREA_BY_EN[hits[0][1]]


def area_from_address(addr):
    """Marker address 'الدائري الثالث, doha, 173' → (areaEn, areaAr) when the first part is a known area."""
    if not addr:
        return None
    first = addr.split(",")[0].strip()
    a = area_in_text([first])
    if a:
        return a
    if AR_LETTERS.search(first):
        return (first, first, None, None)
    return (first, first, None, None)


def make_location(merchant_id, name_en, name_ar, lat, lng, area, approx, used_ids):
    area_en, area_ar = (area[0], area[1]) if area else ("Doha", "الدوحة")
    base = f"{merchant_id}-{slug(area_en)}"
    loc_id, n = base, 1
    while loc_id in used_ids:
        n += 1
        loc_id = f"{base}-{n}"
    used_ids.add(loc_id)
    loc = {
        "id": loc_id,
        "nameAr": f"{name_ar} – {area_ar}" + (" (تقريبي)" if approx else ""),
        "nameEn": f"{name_en} – {area_en}" + (" (approx.)" if approx else ""),
        "lat": round(lat, 6), "lng": round(lng, 6),
        "areaAr": area_ar, "areaEn": area_en,
    }
    if approx:
        loc["approx"] = True
    return loc


# --------------------------------------------------------------------------- main build

def crawl(site, limit=None, log=print):
    ids, meta, total = [], {}, None
    page = 1
    while True:
        text = site.get(f"{BASE}/offers?page={page}", "en", key=f"list_{page}.html")
        cards, total_here = parse_listing(text or "")
        total = total or total_here
        if not cards:
            break
        for c in cards:
            if c["id"] not in meta:
                ids.append(c["id"])
                meta[c["id"]] = c
        log(f"  listing page {page}: {len(cards)} offers (total so far {len(ids)})")
        page += 1
        if limit and len(ids) >= limit:
            ids = ids[:limit]
            break
        if page > 400:
            raise RuntimeError("more than 400 listing pages – pagination parsing is probably broken")
    return ids, meta, total


def fetch_pages(site, ids, log=print):
    """English pages in parallel, then Arabic pages in parallel (Arabic session switched first)."""
    pages = {}

    def one(oid, lang):
        text = site.get(f"{BASE}/offer/{oid}", lang, key=f"offer_{oid}_{lang}.html")
        d = parse_offer_page(text, oid)
        if d and d["lang"] not in (lang, "?"):
            # the session lost its locale: switch again and refetch once (bypassing the cache)
            path = site._cache_path(f"offer_{oid}_{lang}.html")
            if path and os.path.exists(path):
                os.remove(path)
            if lang == "ar":
                site.switch_arabic()
            text = site.get(f"{BASE}/offer/{oid}", lang, key=f"offer_{oid}_{lang}.html")
            d = parse_offer_page(text, oid)
        return oid, lang, d

    for lang in ("en", "ar"):
        if lang == "ar":
            site.switch_arabic()
            probe = site.get(f"{BASE}/offer/{ids[0]}", "ar", key=f"offer_{ids[0]}_ar.html")
            pd = parse_offer_page(probe, ids[0])
            if not pd or pd["lang"] != "ar":
                raise RuntimeError(f"Arabic switch failed; page title was {pd and pd['pageTitle']!r}")
        done = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=site.workers) as ex:
            for oid, lg, d in ex.map(lambda i: one(i, lang), ids):
                pages[(oid, lg)] = d
                done += 1
                if done % 100 == 0:
                    log(f"  {lang}: {done}/{len(ids)} offer pages")
    return pages


def load_markers(site):
    text = site.get(f"{BASE}/map/markers", "en", key="markers.json")
    data = json.loads(text) if text else {"markers": []}
    by_url, by_name = {}, collections.defaultdict(list)
    for mk in data.get("markers", []):
        try:
            lat, lng = float(mk["lat"]), float(mk["lng"])
        except (TypeError, ValueError, KeyError):
            continue
        if mk.get("type") == "offer" and mk.get("url"):
            by_url[mk["url"].rstrip("/")] = mk
        else:
            for nm in (mk.get("title"), mk.get("store")):
                if nm:
                    by_name[norm_key(nm)].append(mk)
    return by_url, by_name, len(data.get("markers", []))


def build(args, log=print):
    site = Site(args.cache, args.delay, args.workers)
    # Previous state, used only to keep ids stable: the previous part when it exists (on this Mac),
    # otherwise the program's offers and merchants already in seed.json (CI checkout has no parts).
    old = {"merchants": [], "offers": []}
    previous = args.previous or args.out
    if os.path.exists(previous):
        old = json.load(open(previous, encoding="utf-8"))
        log(f"    previous part: {len(old.get('offers', []))} offers, {len(old.get('merchants', []))} merchants")
    seed_merchant_ids, seed_merchants, seed_offers = set(), {}, []
    if os.path.exists(args.seed):
        seed = json.load(open(args.seed, encoding="utf-8"))
        seed_offers = seed["offers"]
        seed_merchants = {m["id"]: m for m in seed["merchants"]}
        seed_merchant_ids = set(seed_merchants)
        if not old["offers"]:
            old["offers"] = [o for o in seed["offers"] if o["programId"] == PROGRAM]
            old["merchants"] = [seed_merchants[mid] for mid in {o["merchantId"] for o in old["offers"]} if mid in seed_merchants]
            log(f"    no previous part; ids taken from seed.json: {len(old['offers'])} offers, {len(old['merchants'])} merchants")
    old_merchants = {m["id"]: m for m in old.get("merchants", [])}
    old_offers = {o["id"]: o for o in old.get("offers", [])}
    old_by_source = {}
    for o in old_offers.values():
        src = (o.get("source") or "").rstrip("/")
        m = re.search(r"/offer/(\d+)$", src)
        if m:
            old_by_source[int(m.group(1))] = o

    log("1/5 listing pages")
    ids, listing, site_total = crawl(site, args.limit, log)
    log(f"    {len(ids)} offer ids; site says {site_total} results")
    if site_total and not args.limit and len(ids) != site_total:
        log(f"    WARNING: crawled {len(ids)} ids but the site reports {site_total} results")

    log("2/5 offer pages (English, then Arabic)")
    pages = fetch_pages(site, ids, log)

    log("3/5 map markers")
    markers_by_url, markers_by_name, n_markers = load_markers(site)
    log(f"    {n_markers} markers ({len(markers_by_url)} offer markers with url, {len(markers_by_name)} store names)")

    log("4/5 building merchants and offers")
    merchants, offers = {}, []
    shared_merchants = set()
    subtitles = {}
    used_offer_ids, used_loc_ids = set(), set()
    merchant_for_name = {}
    raw = []
    unmapped_categories = collections.Counter()
    warnings = []

    for oid in ids:
        en, ar = pages.get((oid, "en")), pages.get((oid, "ar"))
        if not en or not en.get("name"):
            warnings.append(f"offer {oid}: English page missing or unparsable – skipped")
            continue
        if not ar or ar.get("lang") != "ar":
            warnings.append(f"offer {oid}: Arabic page missing – Arabic fields fall back to English")
            ar = ar or {}
        raw.append({"en": en, "ar": ar, "listing": listing.get(oid)})
        source = f"{BASE}/offer/{oid}"
        name_en = en["name"]
        name_ar = ar.get("name") or name_en
        if not AR_LETTERS.search(name_ar) and AR_LETTERS.search(ar.get("subtitle") or ""):
            pass  # brand written in Latin letters on the Arabic page too (e.g. DFSK) – keep it
        old_offer = old_by_source.get(oid)

        # ---- merchant
        if old_offer and (old_offer["merchantId"] in old_merchants or old_offer["merchantId"] in seed_merchant_ids):
            mid = old_offer["merchantId"]
            if mid not in old_merchants:
                shared_merchants.add(mid)  # defined by another part / seed.json: reference only
        else:
            key = norm_key(name_en) or norm_key(name_ar)
            mid = merchant_for_name.get(key)
            if not mid:
                mid = slug(name_en) if slug(name_en) != "store" else slug(name_ar) or f"testaahel-store-{oid}"
                base, n = mid, 1
                while (mid in merchants and norm_key(merchants[mid]["nameEn"]) != key) or \
                      (mid not in merchants and mid in seed_merchant_ids and mid not in old_merchants):
                    n += 1
                    mid = f"{base}-{n}"
        merchant_for_name.setdefault(norm_key(name_en), mid)

        site_cat = en.get("category") or (listing.get(oid) or {}).get("category") or ""
        old_m = old_merchants.get(mid)
        category, mapped = map_category(site_cat, name_en, name_ar, old_m["category"] if old_m else None)
        if not mapped and site_cat:
            unmapped_categories[site_cat] += 1

        if mid in shared_merchants:
            m = None
        elif mid not in merchants:
            if old_m:
                m = {
                    "id": mid, "nameEn": old_m["nameEn"], "nameAr": old_m["nameAr"],
                    "category": old_m["category"],  # curated / deduped value wins over the site's
                    "brandAliases": list(old_m.get("brandAliases", [])),
                    "locations": [dict(l) for l in old_m["locations"]],
                }
                for alias in (name_en, name_ar):
                    if is_brand_alias(alias, m):
                        m["brandAliases"].append(alias)
                for l in m["locations"]:
                    used_loc_ids.add(l["id"])
            else:
                m = {"id": mid, "nameEn": name_en, "nameAr": name_ar, "category": category,
                     "brandAliases": [], "locations": []}
                lt = (listing.get(oid) or {}).get("title")
                if is_brand_alias(lt, m):
                    m["brandAliases"].append(lt)
            merchants[mid] = m
        m = merchants.get(mid)
        if m is None:  # shared merchant: names for titles come from seed.json
            sm = seed_merchants.get(mid, {})
            m = {"id": mid, "nameEn": sm.get("nameEn", name_en), "nameAr": sm.get("nameAr", name_ar), "locations": [None]}

        # ---- locations for merchants new to the part
        if not old_m and mid not in shared_merchants:
            texts = [name_en, name_ar, en.get("subtitle"), ar.get("subtitle"), en.get("shortDescription"),
                     en.get("description"), ar.get("description"), en.get("terms"), ar.get("terms"),
                     en.get("facilities")]
            found = []
            mk = markers_by_url.get(source)
            if mk:
                found.append((float(mk["lat"]), float(mk["lng"]), area_from_address(mk.get("address")), "offer marker"))
            for nm in (name_ar, name_en, (listing.get(oid) or {}).get("title")):
                for smk in markers_by_name.get(norm_key(nm or ""), []):
                    found.append((float(smk["lat"]), float(smk["lng"]), area_from_address(smk.get("address")), "store marker"))
            if not found and en.get("mapsUrl"):
                coords = coords_from_maps_url(en["mapsUrl"])
                if not coords:
                    coords = coords_from_maps_url(site.resolve_redirect(en["mapsUrl"]))
                if coords:
                    found.append((coords[0], coords[1], None, "maps link"))
            seen = set()
            for lat, lng, area, how in found:
                k = (round(lat, 4), round(lng, 4))
                if k in seen:
                    continue
                seen.add(k)
                if area is None or area[2] is None and area[0] == area[1]:
                    near = nearest_area(lat, lng) if in_qatar(lat, lng) else None
                    if area is None:
                        area = near or area_in_text(texts) or (("Doha", "الدوحة", *DOHA_DEFAULT) if in_qatar(lat, lng) else None)
                    elif near:
                        area = near
                if area is None:
                    area = ("Abroad", "خارج قطر", lat, lng)
                m["locations"].append(make_location(mid, m["nameEn"], m["nameAr"], lat, lng, area, False, used_loc_ids))
            if not m["locations"]:
                area = area_in_text(texts)
                if area:
                    m["locations"].append(make_location(mid, m["nameEn"], m["nameAr"], area[2], area[3], area, True, used_loc_ids))
                elif category == "hotel" and site_cat.lower() in ("external offers",):
                    warnings.append(f"offer {oid} ({name_en}): foreign offer with unknown city – placed at 0,0 (merge_seed drops it)")
                    m["locations"].append(make_location(mid, m["nameEn"], m["nameAr"], 0.0, 0.0, ("Abroad", "خارج قطر"), True, used_loc_ids))
                else:
                    m["locations"].append(make_location(mid, m["nameEn"], m["nameAr"], *DOHA_DEFAULT, ("Doha", "الدوحة"), True, used_loc_ids))

        # ---- offer
        badge = en.get("badge") or (listing.get(oid) or {}).get("badge") or ""
        dtype, dval, kind, hi = parse_badge(badge)
        if dtype == "other":
            if BOGO.search((en.get("subtitle") or "") + " " + (ar.get("subtitle") or "")):
                dtype, dval, kind = "bogo", None, "exact"
            else:
                # 3 in 4 badges just say "Special Offer"; the percentage is in the text (first crawl did the same)
                inferred = percent_from_text([badge, en.get("subtitle"), ar.get("subtitle"), en.get("shortDescription"),
                                              en.get("description"), ar.get("shortDescription"), ar.get("description")])
                if inferred:
                    kind, dval, hi = inferred
                    dtype = "percent"
        subtitle_en, subtitle_ar = en.get("subtitle") or "", ar.get("subtitle") or ""
        upto_flag = (dtype in ("percent", "fixed") and kind == "upto")
        if dtype == "percent":
            if kind == "range":
                lbl = f"{fmt_num(dval)}–{fmt_num(hi)}%"
                title_ar = f"خصم {lbl} لحاملي بطاقة تستاهل في {m['nameAr']}"
                title_en = f"{lbl} off for Testaahel cardholders at {m['nameEn']}"
            elif kind == "upto":
                title_ar = f"خصم يصل إلى {fmt_num(dval)}% لحاملي بطاقة تستاهل في {m['nameAr']}"
                title_en = f"Up to {fmt_num(dval)}% off for Testaahel cardholders at {m['nameEn']}"
            else:
                title_ar = f"خصم {fmt_num(dval)}% لحاملي بطاقة تستاهل في {m['nameAr']}"
                title_en = f"{fmt_num(dval)}% off for Testaahel cardholders at {m['nameEn']}"
        elif dtype == "fixed":
            upto_ar, upto_en = ("يصل إلى " if kind == "upto" else ""), ("Up to " if kind == "upto" else "")
            title_ar = f"خصم {upto_ar}{fmt_num(dval)} ر.ق لحاملي بطاقة تستاهل في {m['nameAr']}"
            title_en = f"{upto_en}{fmt_num(dval)} QAR off for Testaahel cardholders at {m['nameEn']}"
        elif dtype == "bogo":
            title_ar = f"اشترِ واحدة واحصل على الثانية مجانًا لحاملي بطاقة تستاهل في {m['nameAr']}"
            title_en = f"Buy 1 get 1 free for Testaahel cardholders at {m['nameEn']}"
        else:
            title_ar = f"عرض خاص لحاملي بطاقة تستاهل في {m['nameAr']}"
            title_en = (f"{badge} – {m['nameEn']}" if badge and badge.lower() not in ("special offer", "special offers")
                        else f"Special offer for Testaahel cardholders at {m['nameEn']}")
        # The site's own one-line subtitle says what the discount is ON ("30% on vehicle-care fees, 5% on
        # products"); a template title hides that and reads as "30% at Woqod". Use it when it states a figure.
        def headline(sub):
            s = (sub or "").strip()
            first = re.split(r"(?<=[.!؟?])\s+", s)[0].strip().rstrip(".")
            return first if first and len(first) <= 120 and (PCT_IN_TEXT.search(first) or re.search(r"خصم|discount|off\b|free|مجان", first, re.I)) else None
        title_ar = headline(subtitle_ar) or title_ar
        title_en = headline(subtitle_en) or title_en

        def conditions(sub, short, full, fac, terms, suffix, pdf_note, lang):
            parts = []
            for p in (sub, full or short, fac, terms):
                p = (p or "").strip()
                if p and p not in parts and p.rstrip(".") != (sub or "").rstrip("."):
                    parts.append(p if p[-1] in ".!?؟" else p + ".")
                elif p and p == sub:
                    parts.append(p if p[-1] in ".!?؟" else p + ".")
            if en.get("phone"):
                parts.append(("الهاتف: " if lang == "ar" else "Phone: ") + en["phone"] + ".")
            if en.get("zone"):
                parts.append(("المنطقة " if lang == "ar" else "Zone ") + en["zone"] + ".")
            parts.append(suffix)
            if en.get("pdf"):
                parts.append(pdf_note)
            text = " ".join(dict.fromkeys(parts))
            return WS.sub(" ", text).strip()

        cond_en = conditions(subtitle_en, en.get("shortDescription"), en.get("description"), en.get("facilities"),
                             en.get("terms"), SUFFIX_EN, PDF_EN, "en")
        cond_ar = conditions(subtitle_ar, ar.get("shortDescription"), ar.get("description"), ar.get("facilities"),
                             ar.get("terms"), SUFFIX_AR, PDF_AR, "ar")
        if dtype == "percent":
            cond_ar += " النسبة مأخوذة من وصف العرض على موقع تستاهل."
            cond_en += " Percentage taken from the offer description on testaahel.qa."

        if old_offer:
            offer_id = old_offer["id"]
        else:
            offer_id, n = f"{PROGRAM}-{mid}", 1
            while offer_id in used_offer_ids or offer_id in old_offers:
                n += 1
                offer_id = f"{PROGRAM}-{mid}-{n}"
        used_offer_ids.add(offer_id)
        subtitles[offer_id] = (subtitle_en, subtitle_ar)
        offers.append({
            "id": offer_id, "programId": PROGRAM, "merchantId": mid,
            "discountType": dtype, "discountValue": dval,
            "discountMax": (hi if (dtype == "percent" and kind == "range" and hi) else None), "upTo": upto_flag,
            "titleAr": title_ar, "titleEn": title_en,
            "conditionsAr": cond_ar, "conditionsEn": cond_en,
            "validFrom": None, "validUntil": None, "tiers": [],
            "source": source, "verified": True, "lastCheckedAt": TODAY,
        })

    # Several offers of one merchant with the same figure would collide on (merchant, titleAr, value),
    # which the app's tests forbid: name the outlet/offer from the site's subtitle, and collapse offers
    # the site itself lists twice.
    groups = collections.defaultdict(list)
    for o in offers:
        groups[(o["merchantId"], o["titleAr"], o["discountValue"])].append(o)
    for key, group in groups.items():
        if len(group) < 2:
            continue
        for o in group:
            sub_en, sub_ar = subtitles.get(o["id"], ("", ""))
            m = merchants.get(o["merchantId"]) or seed_merchants.get(o["merchantId"]) or {}
            if sub_ar and norm_key(sub_ar) != norm_key(m.get("nameAr", "")) and not JUNK_ALIAS.search(sub_ar):
                o["titleAr"] = f"{o['titleAr']} – {sub_ar}"
                o["titleEn"] = f"{o['titleEn']} – {sub_en or sub_ar}"
            elif sub_en and norm_key(sub_en) != norm_key(m.get("nameEn", "")) and not JUNK_ALIAS.search(sub_en):
                o["titleAr"] = f"{o['titleAr']} – {sub_en}"
                o["titleEn"] = f"{o['titleEn']} – {sub_en}"
        seen = {}
        for o in sorted(group, key=lambda o: (len(o["id"]), o["id"])):
            k = (o["titleAr"], o["discountValue"])
            if k in seen:
                warnings.append(f"{o['id']} ({o['source']}) duplicates {seen[k]['id']} on the site – collapsed")
                seen[k]["conditionsAr"] += f" مكرر على الموقع: {o['source']}."
                seen[k]["conditionsEn"] += f" Also listed as {o['source']}."
                offers.remove(o)
            else:
                seen[k] = o

    # Offers of the previous part that did not come from an /offer/<id> page (added by hand from the
    # homepage or a PDF) are not on the listing, so they are carried over unchanged, not dropped.
    hand_added = list(old_offers.values()) + [o for o in seed_offers if o["programId"] == PROGRAM and o["id"] not in old_offers]
    for o in hand_added:
        src = (o.get("source") or "").rstrip("/")
        if re.search(r"/offer/\d+$", src) or o["id"] in used_offer_ids:
            continue
        offers.append(o)
        used_offer_ids.add(o["id"])
        warnings.append(f"{o['id']} kept unchanged: hand-added offer (source {o.get('source')}), not on the offers listing")
        om = old_merchants.get(o["merchantId"])
        if om and o["merchantId"] not in merchants and o["merchantId"] not in seed_merchant_ids:
            merchants[om["id"]] = om

    # merchants that only had vanished offers are dropped with them
    part = {"merchants": sorted(merchants.values(), key=lambda m: m["id"]),
            "offers": sorted(offers, key=lambda o: o["id"])}

    # ---- report
    new_ids = {o["id"] for o in offers}
    old_ids = set(old_offers)
    added = sorted(new_ids - old_ids)
    removed = sorted(old_ids - new_ids)
    changed, unchanged = [], 0
    for o in offers:
        if o["id"] in old_offers:
            a = {k: v for k, v in o.items() if k not in ("lastCheckedAt", "conditionsAr", "conditionsEn")}
            b = {k: v for k, v in old_offers[o["id"]].items() if k not in ("lastCheckedAt", "conditionsAr", "conditionsEn")}
            if a != b:
                changed.append(o["id"])
            else:
                unchanged += 1
    report = {
        "date": TODAY, "siteTotal": site_total, "crawled": len(ids), "requests": site.requests,
        "offersBefore": len(old_offers), "offersAfter": len(offers),
        "merchantsBefore": len(old_merchants), "merchantsAfter": len(merchants),
        "added": added, "removed": removed, "changed": changed, "unchanged": unchanged,
        "newMerchants": sorted(set(merchants) - set(old_merchants)),
        "droppedMerchants": sorted(set(old_merchants) - set(merchants)),
        "unmappedCategories": dict(unmapped_categories), "warnings": warnings,
    }
    return part, report, raw


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=DEFAULT_PART,
                    help="seed part to rebuild, or a parts directory (default: research_notes/.../seed_parts/testaahel.json)")
    ap.add_argument("--previous", default=None, help="previous part to take ids from (default: --out)")
    ap.add_argument("--seed", default=DEFAULT_SEED, help="app seed.json (only read, to avoid merchant id clashes)")
    ap.add_argument("--cache", default=None, help="directory for raw HTML/JSON cache (re-runs skip the network)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--delay", type=float, default=0.3, help="seconds between requests (global)")
    ap.add_argument("--limit", type=int, default=None, help="only the first N offers (for testing)")
    ap.add_argument("--dry-run", action="store_true", help="do not write the part")
    ap.add_argument("--raw-out", default=None, help="also dump the parsed pages as JSON here")
    ap.add_argument("--report-out", default=None, help="write the JSON report here")
    args = ap.parse_args()
    if os.path.isdir(args.out) or args.out.endswith(os.sep):
        args.out = os.path.join(args.out, "testaahel.json")  # refresh_all.sh / CI pass the parts directory

    part, report, raw = build(args)

    print("5/5 result")
    print(f"    site total {report['siteTotal']}, crawled {report['crawled']}, HTTP requests {report['requests']}")
    print(f"    offers {report['offersBefore']} -> {report['offersAfter']}: "
          f"+{len(report['added'])} added, -{len(report['removed'])} removed, "
          f"{len(report['changed'])} changed, {report['unchanged']} unchanged")
    print(f"    merchants {report['merchantsBefore']} -> {report['merchantsAfter']} "
          f"(+{len(report['newMerchants'])} new, -{len(report['droppedMerchants'])} dropped)")
    if report["unmappedCategories"]:
        print("    UNMAPPED site categories (fell back to old/other):", report["unmappedCategories"])
    for w in report["warnings"][:40]:
        print("    !", w)
    if len(report["warnings"]) > 40:
        print(f"    ... {len(report['warnings']) - 40} more warnings")

    if args.raw_out:
        json.dump(raw, open(args.raw_out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if args.report_out:
        json.dump(report, open(args.report_out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if args.dry_run or args.limit:
        print("    dry run / limited crawl: part NOT written")
        return 0
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    if os.path.exists(args.out):
        backup = args.out + ".bak-" + dt.datetime.now().strftime("%Y-%m-%d-%H%M%S")
        shutil.copy2(args.out, backup)
        print(f"    backup: {backup}")
    json.dump(part, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"    wrote {args.out}")
    print("    next: python3 -I tools/merge_seed.py Bitaqati/Resources/seed.json <seed_parts> --prune-program testaahel"
          " && python3 -I tools/dedupe_merchants.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
