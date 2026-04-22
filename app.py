"""
Mower Finder - Web app for sourcing used walking greens mowers.
Finds Toro Greensmaster & John Deere SL/E-Cut listings across Google, eBay,
GovDeals, and Reddit. Flags bulk sellers (3+ units) automatically.
Backed by Supabase for persistent storage.
"""
import os
import re
import json
import hashlib
import html
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import streamlit as st
import requests
from bs4 import BeautifulSoup
import pandas as pd
from supabase import create_client, Client


# ============================================================================
# Page config + styling
# ============================================================================

st.set_page_config(
    page_title="Mower Finder",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    .stApp { background: #0f1419; }
    .main .block-container { padding-top: 2rem; max-width: 1400px; }
    [data-testid="stSidebar"] { background: #1a2028; }
    .badge {
        display: inline-block; padding: 2px 10px; border-radius: 4px;
        font-size: 11px; font-weight: 700; text-transform: uppercase;
        letter-spacing: .04em; margin-right: 6px;
    }
    .badge-toro { background: #d00; color: #fff; }
    .badge-jd { background: #367c2b; color: #fff; }
    .badge-mixed { background: #888; color: #fff; }
    .badge-bulk { background: #ff6b35; color: #000; }
    .big-qty { font-size: 32px; font-weight: 800; color: #ff6b35; line-height: 1; }
    .lead-title a { color: #e4e7eb; text-decoration: none; font-weight: 600; }
    .lead-title a:hover { color: #00c389; }
    .lead-meta { color: #7a8694; font-size: 13px; }
    .lead-price { color: #00c389; font-weight: 600; }
    div[data-testid="stMetricValue"] { color: #00c389; }
</style>
""", unsafe_allow_html=True)


# ============================================================================
# Supabase client
# ============================================================================

LISTINGS_TABLE = "mower_listings"
SETTINGS_TABLE = "mower_settings"


def _get_secret(name):
    try:
        return st.secrets.get(name, os.environ.get(name, ""))
    except Exception:
        return os.environ.get(name, "")


@st.cache_resource
def get_supabase():
    url = _get_secret("SUPABASE_URL")
    key = _get_secret("SUPABASE_SERVICE_KEY")
    if not url or not key:
        return None
    try:
        return create_client(url, key)
    except Exception as e:
        st.error(f"Could not connect to Supabase: {e}")
        return None


def check_db_ready():
    """Verify Supabase is connected and tables exist. Returns (ok, error_message)."""
    supa = get_supabase()
    if supa is None:
        return False, "SUPABASE_URL or SUPABASE_SERVICE_KEY not set in Streamlit secrets."
    try:
        supa.table(LISTINGS_TABLE).select("id", count="exact", head=True).execute()
        supa.table(SETTINGS_TABLE).select("key", count="exact", head=True).execute()
        return True, None
    except Exception as e:
        msg = str(e)
        if "relation" in msg.lower() and "does not exist" in msg.lower():
            return False, "Tables not created yet. Run the SQL from schema.sql in your Supabase SQL Editor."
        if "pgrst" in msg.lower() or "42p01" in msg.lower():
            return False, "Tables not created yet. Run the SQL from schema.sql in your Supabase SQL Editor."
        return False, f"Database error: {msg}"


def fingerprint_listing(url, title):
    u = (url or "").lower().rstrip("/").split("?")[0]
    t = (title or "").lower()[:80]
    return hashlib.md5(f"{u}|{t}".encode()).hexdigest()


# ============================================================================
# Settings (stored in mower_settings table)
# ============================================================================

def get_setting(key, default=None):
    supa = get_supabase()
    if supa is None:
        return default
    try:
        resp = supa.table(SETTINGS_TABLE).select("value").eq("key", key).limit(1).execute()
        if resp.data:
            return resp.data[0]["value"]
    except Exception:
        pass
    return default


def set_setting(key, value):
    supa = get_supabase()
    if supa is None:
        return
    try:
        supa.table(SETTINGS_TABLE).upsert(
            {"key": key, "value": value, "updated_at": datetime.now(timezone.utc).isoformat()},
            on_conflict="key",
        ).execute()
    except Exception as e:
        st.error(f"Settings save failed: {e}")


# ============================================================================
# Defaults (editable in Settings tab, stored in Supabase)
# ============================================================================

DEFAULT_TORO = [
    "Greensmaster 1000", "Greensmaster 1600", "Greensmaster 1000 Diesel",
    "Greensmaster Flex 1820", "Greensmaster Flex 2120",
    "Greensmaster eFlex 1800", "Greensmaster eFlex 2100",
    "Greensmaster eFlex 1021", "Greensmaster eFlex 2120",
    "Greensmaster 800",
]
DEFAULT_JD = [
    "220 E-Cut", "220SL", "260SL", "180SL", "180 E-Cut",
    "PrecisionCut 180", "PrecisionCut 220", "PrecisionCut 260",
    "220 A-Model", "220 B-Model", "220 C-Model",
]
DEFAULT_BASE_QUERIES = [
    "used walking greens mower for sale",
    "used walk behind greens mower for sale",
    "golf course mower fleet sale",
    "golf course closing sale equipment",
    "lot of greens mowers for sale",
    "golf course equipment auction walking greens",
    "used greens mower fleet liquidation",
]
DEFAULT_SUBREDDITS = ["golfcourse", "turfgrass", "Golf", "turfequipment"]


# ============================================================================
# Quantity / brand / relevance detection
# ============================================================================

WORD_NUMS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20,
}

UNIT_RX = (r"(?:units?|mowers?|machines?|walk[-\s]*behinds?|walking[-\s]*greens?|"
           r"greens[-\s]*mowers?|pieces?|pcs?|ea(?:ch)?|reels?|greensmasters?)")
BRAND_RX = r"(?:toro|john\s*deere|jd|greensmaster|e[-\s]?cut|sl|precisioncut|flex|eflex)"

QTY_PATTERNS = [
    re.compile(rf"(\d+)\s*(?:x\s*)?{UNIT_RX}", re.I),
    re.compile(r"(?:quantity|qty|lot\s*of|total\s*of|fleet\s*of|set\s*of|group\s*of)[\s:]*?(\d+)", re.I),
    re.compile(rf"\((\d+)\)\s*{BRAND_RX}", re.I),
    re.compile(rf"(\d+)\s*(?:total|available|in\s*stock|on\s*hand|ready)\s*{UNIT_RX}?", re.I),
    re.compile(rf"\b(\d+)\s+(?:used\s+|new\s+|of\s+)?{BRAND_RX}\b(?:[\s\-a-z0-9]+?){UNIT_RX}", re.I),
    re.compile(rf"(?:^|[\.\|\-]\s*)(\d+)\s+(?:used\s+|new\s+)?{BRAND_RX}\b", re.I),
]

BULK_HINT_RX = re.compile(
    r"\b(fleet|multiple|lot\s*of|bulk|package|(?:closing|liquidation)\s*sale|course\s*closing|"
    r"complete\s*fleet|entire\s*fleet|full\s*fleet|several\s+greens)\b", re.I,
)


def extract_quantity(*texts):
    blob = " \n ".join(t for t in texts if t).lower()
    qty = 1
    for pat in QTY_PATTERNS:
        for m in pat.finditer(blob):
            try:
                n = int(m.group(1))
                if 1 <= n <= 100:
                    qty = max(qty, n)
            except (ValueError, IndexError):
                continue
    for word, n in WORD_NUMS.items():
        if re.search(rf"\b{word}\b(?:\s+[a-z0-9\-]+){{0,6}}\s+{UNIT_RX}", blob, re.I):
            qty = max(qty, n)
    return qty


def detect_brand(*texts):
    blob = " ".join(t for t in texts if t).lower()
    has_toro = bool(re.search(r"\btoro\b|\bgreensmaster\b|\beflex\b", blob))
    has_jd = bool(re.search(
        r"\bjohn\s*deere\b|\bjohn-deere\b|\bdeere\b|\bprecisioncut\b|\be[-\s]?cut\b|"
        r"\b\d{3}\s*sl\b|\bjd\s*\d{3}\b", blob))
    if has_toro and has_jd: return "mixed"
    if has_toro: return "toro"
    if has_jd: return "john_deere"
    return "unknown"


def is_relevant(title, snippet=""):
    text = (title + " " + (snippet or "")).lower()
    has_greens = bool(re.search(
        r"\bgreens?\b|\bwalk[-\s]*behind\b|\bwalking[-\s]*greens?\b|greensmaster|"
        r"precisioncut|e[-\s]?cut|\d{3}\s*sl\b", text))
    excluded = bool(re.search(
        r"\btoy\b|\bmodel\s*car\b|\b(?:rc|radio\s*control)\b|\bminiature\b|"
        r"z[-\s]?trak|\bzero[-\s]*turn\b|\briding\s*mower\b|\bpush\s*mower\b|"
        r"\brotary\s*mower\b|\blawn\s*tractor\b", text))
    return has_greens and not excluded


# ============================================================================
# Geographic filter: accept only US listings, excluding Hawaii and Alaska
# ============================================================================

NON_US_COUNTRY_RX = re.compile(
    r"\b(canada|canadian|united\s*kingdom|\bu\.?k\.?\b|england|scotland|wales|ireland|"
    r"australia|new\s*zealand|germany|france|italy|spain|netherlands|belgium|"
    r"japan|china|mexico|brazil|philippines|south\s*africa|"
    r"ontario|quebec|alberta|manitoba|british\s*columbia|nova\s*scotia|saskatchewan)\b",
    re.I,
)

HI_AK_NAME_RX = re.compile(
    r"\bhawaii\b|\balaska\b|"
    r"honolulu|anchorage|fairbanks|juneau|\bhilo\b|"
    r"\bmaui\b|\bkauai\b|\boahu\b|kodiak",
    re.I,
)

# State code "HI" or "AK" in a location-like context: ", HI" / "- AK" / "(HI)" etc.
HI_AK_CODE_RX = re.compile(
    r"(?:^|[,\-\(\s])(?:HI|AK)(?:\s|,|\)|\.|$|\d)"
)


def location_acceptable(title, location):
    """
    Return False if the listing is clearly from outside the continental US
    (non-US countries, Hawaii, or Alaska). Return True if it looks US mainland
    or the location is unknown.
    """
    for field in [location, title]:
        if not field:
            continue
        s = str(field)
        if NON_US_COUNTRY_RX.search(s):
            return False
        if HI_AK_NAME_RX.search(s):
            return False
        if HI_AK_CODE_RX.search(s):
            return False
    return True


# ============================================================================
# Source adapters
# ============================================================================

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 13_0) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def search_serpapi(queries, api_key, results_per_query=20, progress_cb=None):
    out = []
    for i, q in enumerate(queries):
        if progress_cb: progress_cb(i, len(queries), f"Google: {q}")
        try:
            r = requests.get(
                "https://serpapi.com/search",
                params={"q": q, "api_key": api_key, "num": results_per_query,
                        "engine": "google", "hl": "en", "gl": "us",
                        "location": "United States"},
                timeout=30,
            )
            data = r.json()
            for item in data.get("organic_results", []):
                out.append({
                    "source": "google", "title": item.get("title", ""),
                    "url": item.get("link", ""), "snippet": item.get("snippet", ""),
                    "query": q,
                })
            time.sleep(0.5)
        except Exception as e:
            if progress_cb: progress_cb(i, len(queries), f"Google error: {e}")
    return out


def search_ebay(queries, max_pages=2, progress_cb=None):
    out = []
    for i, q in enumerate(queries):
        if progress_cb: progress_cb(i, len(queries), f"eBay: {q}")
        for page in range(1, max_pages + 1):
            try:
                url = f"https://www.ebay.com/sch/i.html?_nkw={quote_plus(q)}&_pgn={page}&LH_Sold=0&LH_PrefLoc=1"
                r = requests.get(url, headers=HEADERS, timeout=20)
                soup = BeautifulSoup(r.text, "html.parser")
                for item in soup.select("li.s-item"):
                    link_el = item.select_one("a.s-item__link")
                    title_el = item.select_one(".s-item__title")
                    price_el = item.select_one(".s-item__price")
                    loc_el = item.select_one(".s-item__location")
                    subtitle_el = item.select_one(".s-item__subtitle")
                    if not link_el or not title_el: continue
                    title = title_el.get_text(strip=True)
                    if title.lower().startswith("shop on ebay"): continue
                    out.append({
                        "source": "ebay", "title": title,
                        "url": link_el["href"].split("?")[0],
                        "snippet": subtitle_el.get_text(" ", strip=True) if subtitle_el else "",
                        "price": price_el.get_text(strip=True) if price_el else None,
                        "location": loc_el.get_text(strip=True) if loc_el else None,
                        "query": q,
                    })
                time.sleep(1)
            except Exception as e:
                if progress_cb: progress_cb(i, len(queries), f"eBay error: {e}")
    return out


def search_govdeals(queries, progress_cb=None):
    out = []
    for i, q in enumerate(queries):
        if progress_cb: progress_cb(i, len(queries), f"GovDeals: {q}")
        try:
            url = ("https://www.govdeals.com/index.cfm?fa=Main.AdvSearchResultsNew"
                   f"&searchPg=Cat&kWord={quote_plus(q)}&kWordSelect=2&sortBy=ad")
            r = requests.get(url, headers=HEADERS, timeout=25)
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.select('a[href*="fa=Main.Item"]'):
                title = a.get_text(" ", strip=True)
                href = a["href"]
                if not href.startswith("http"):
                    href = "https://www.govdeals.com" + href
                if len(title) < 10: continue
                out.append({"source": "govdeals", "title": title, "url": href,
                            "snippet": "", "query": q})
            time.sleep(1)
        except Exception as e:
            if progress_cb: progress_cb(i, len(queries), f"GovDeals error: {e}")
    return out


def search_reddit(queries, subreddits, ua, progress_cb=None):
    out = []
    total = len(subreddits) * len(queries)
    step = 0
    for sub in subreddits:
        for q in queries:
            step += 1
            if progress_cb: progress_cb(step - 1, total, f"Reddit r/{sub}: {q}")
            try:
                r = requests.get(
                    f"https://www.reddit.com/r/{sub}/search.json",
                    headers={"User-Agent": ua},
                    params={"q": q, "restrict_sr": 1, "sort": "new", "limit": 25, "t": "year"},
                    timeout=20,
                )
                if r.status_code != 200: continue
                data = r.json()
                for child in data.get("data", {}).get("children", []):
                    p = child.get("data", {})
                    out.append({
                        "source": f"reddit/{sub}", "title": p.get("title", ""),
                        "url": "https://reddit.com" + p.get("permalink", ""),
                        "snippet": (p.get("selftext", "") or "")[:500],
                        "query": q,
                    })
                time.sleep(1.5)
            except Exception as e:
                if progress_cb: progress_cb(step - 1, total, f"Reddit error: {e}")
    return out


def build_queries(toro_models, jd_models, base_queries):
    queries = list(base_queries)
    queries.extend(f"used Toro {m} for sale" for m in toro_models)
    queries.extend(f"used John Deere {m} for sale" for m in jd_models)
    return queries


def enrich_and_store(raw_results, bulk_threshold=3):
    """Filter, enrich, and batch-upsert listings to Supabase."""
    enriched = []
    for r in raw_results:
        texts = [r.get("title", ""), r.get("snippet", "")]
        if not is_relevant(*texts): continue
        if not location_acceptable(r.get("title", ""), r.get("location", "")):
            continue
        r["brand"] = detect_brand(*texts)
        if r["brand"] == "unknown": continue
        r["quantity"] = extract_quantity(*texts)
        has_hint = bool(BULK_HINT_RX.search(" ".join(texts).lower()))
        r["is_bulk"] = r["quantity"] >= bulk_threshold or has_hint
        r["fingerprint"] = fingerprint_listing(r["url"], r["title"])
        enriched.append(r)

    if not enriched:
        return 0, 0, 0

    supa = get_supabase()
    if supa is None:
        st.error("No database connection — cannot save results.")
        return 0, 0, 0

    # Dedupe by fingerprint within this batch (same URL appearing in multiple queries)
    seen_fps = set()
    dedup = []
    for r in enriched:
        if r["fingerprint"] not in seen_fps:
            seen_fps.add(r["fingerprint"])
            dedup.append(r)

    all_fps = [r["fingerprint"] for r in dedup]
    now_iso = datetime.now(timezone.utc).isoformat()

    # Find which fingerprints already exist (batched queries to stay under URL limits)
    existing_fps = set()
    CHUNK = 200
    for i in range(0, len(all_fps), CHUNK):
        chunk = all_fps[i:i + CHUNK]
        resp = supa.table(LISTINGS_TABLE).select("fingerprint").in_("fingerprint", chunk).execute()
        existing_fps.update(row["fingerprint"] for row in resp.data)

    # Refresh last_seen for existing
    existing_list = list(existing_fps)
    for i in range(0, len(existing_list), CHUNK):
        chunk = existing_list[i:i + CHUNK]
        supa.table(LISTINGS_TABLE).update({"last_seen": now_iso}).in_("fingerprint", chunk).execute()

    # Build insert rows for new ones
    new_rows = []
    for r in dedup:
        if r["fingerprint"] in existing_fps:
            continue
        new_rows.append({
            "fingerprint": r["fingerprint"],
            "source": r["source"],
            "title": r["title"],
            "url": r["url"],
            "snippet": r.get("snippet"),
            "price": r.get("price"),
            "location": r.get("location"),
            "brand": r.get("brand", "unknown"),
            "quantity": int(r.get("quantity", 1)),
            "is_bulk": bool(r.get("is_bulk")),
            "query": r.get("query"),
        })

    if new_rows:
        INSERT_CHUNK = 500
        for i in range(0, len(new_rows), INSERT_CHUNK):
            supa.table(LISTINGS_TABLE).insert(new_rows[i:i + INSERT_CHUNK]).execute()

    set_setting("last_search", now_iso)

    new_count = len(new_rows)
    bulk_new = sum(1 for r in new_rows if r["is_bulk"])
    return new_count, bulk_new, len(dedup)


def load_demo_data():
    demos = [
        {"source": "google", "title": "Liquidation sale: fleet of 6 Toro Greensmaster 1600 walk behind greens mowers",
         "url": "https://example.com/sale-123", "snippet": "Course closing, must sell entire fleet of six Greensmaster walking greens mowers. Serviced annually.",
         "price": "$12,000 each", "location": "Scottsdale, AZ", "query": "fleet of greens mowers"},
        {"source": "ebay", "title": "(4) Toro Greensmaster Flex 1820 Walk Behind Reel Greens Mower",
         "url": "https://ebay.com/itm/1111", "snippet": "4 units available, all functional",
         "price": "$8,500", "location": "Orlando, FL", "query": "Toro Greensmaster Flex 1820"},
        {"source": "govdeals", "title": "3 John Deere 220 E-Cut Walking Greens Mowers - Municipal Golf Course",
         "url": "https://govdeals.com/itm/2222", "snippet": "Three JD 220 E-Cut mowers from city golf course",
         "price": "$4,200", "location": "Dayton, OH", "query": "John Deere 220 E-Cut"},
        {"source": "reddit/golfcourse", "title": "Anyone know a buyer for two Toro Greensmaster 1000s?",
         "url": "https://reddit.com/r/golfcourse/p3", "snippet": "We have 2 Greensmaster 1000 units retiring this spring",
         "price": None, "location": None, "query": "Toro Greensmaster 1000"},
        {"source": "google", "title": "Used John Deere 220SL walking greens mower - single unit",
         "url": "https://example.com/jd-220sl", "snippet": "Single JD 220SL in excellent shape, low hours",
         "price": "$5,800", "location": "Charleston, SC", "query": "John Deere 220SL"},
        {"source": "ebay", "title": "Toro Greensmaster eFlex 1021 Lithium Battery Walking Greens Mower",
         "url": "https://ebay.com/itm/3333", "snippet": "Modern electric walking greens mower, single unit",
         "price": "$9,900", "location": "Dallas, TX", "query": "Toro Greensmaster eFlex"},
    ]
    return enrich_and_store(demos, bulk_threshold=3)


# ============================================================================
# Data access (Supabase reads)
# ============================================================================

def fetch_listings(bulk_only=False, brand=None, status=None, days=None, search=None, limit=500):
    supa = get_supabase()
    if supa is None:
        return []
    q = supa.table(LISTINGS_TABLE).select("*")
    if bulk_only:
        q = q.eq("is_bulk", True)
    if brand and brand not in ("All", None):
        q = q.eq("brand", brand)
    if status and status not in ("All", None):
        q = q.eq("status", status)
    if days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        q = q.gte("first_seen", cutoff)
    if search:
        # Postgrest `or` filter: sanitize commas/parens that would break the syntax
        safe = search.replace(",", " ").replace("(", " ").replace(")", " ")
        q = q.or_(f"title.ilike.%{safe}%,snippet.ilike.%{safe}%")
    q = q.order("is_bulk", desc=True).order("first_seen", desc=True).limit(limit)
    try:
        return q.execute().data or []
    except Exception as e:
        st.error(f"Query failed: {e}")
        return []


def fetch_all_listings():
    """Paginated fetch for CSV export."""
    supa = get_supabase()
    if supa is None:
        return []
    all_data = []
    start = 0
    PAGE = 1000
    while True:
        resp = (supa.table(LISTINGS_TABLE)
                .select("*")
                .order("is_bulk", desc=True).order("first_seen", desc=True)
                .range(start, start + PAGE - 1).execute())
        chunk = resp.data or []
        all_data.extend(chunk)
        if len(chunk) < PAGE:
            break
        start += PAGE
    return all_data


def get_stats():
    supa = get_supabase()
    if supa is None:
        return {"total": 0, "bulk_total": 0, "new_count": 0, "bulk_new": 0}

    def count(q):
        try:
            return q.execute().count or 0
        except Exception:
            return 0

    t = supa.table(LISTINGS_TABLE)
    return {
        "total":      count(t.select("id", count="exact", head=True)),
        "bulk_total": count(t.select("id", count="exact", head=True).eq("is_bulk", True)),
        "new_count":  count(t.select("id", count="exact", head=True).eq("status", "new")),
        "bulk_new":   count(t.select("id", count="exact", head=True).eq("is_bulk", True).eq("status", "new")),
    }


def update_listing_status(listing_id, new_status, notes=None):
    supa = get_supabase()
    if supa is None:
        return
    payload = {"status": new_status}
    if notes is not None:
        payload["notes"] = notes
    try:
        supa.table(LISTINGS_TABLE).update(payload).eq("id", listing_id).execute()
    except Exception as e:
        st.error(f"Status update failed: {e}")


def delete_all_listings():
    supa = get_supabase()
    if supa is None:
        return
    supa.table(LISTINGS_TABLE).delete().neq("id", 0).execute()


# ============================================================================
# UI helpers
# ============================================================================

def format_date(iso_str):
    try:
        dt = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - dt
        if delta.days == 0:
            hours = delta.seconds // 3600
            if hours == 0: return "just now"
            return f"{hours}h ago"
        if delta.days == 1: return "yesterday"
        if delta.days < 7: return f"{delta.days}d ago"
        return dt.strftime("%b %d")
    except Exception:
        return str(iso_str)[:10] if iso_str else ""


def render_lead_card(lead):
    brand = lead.get("brand", "unknown")
    brand_label = {"toro": "Toro", "john_deere": "John Deere", "mixed": "Mixed"}.get(brand, "?")
    brand_class = {"toro": "badge-toro", "john_deere": "badge-jd", "mixed": "badge-mixed"}.get(brand, "")

    with st.container(border=True):
        c1, c2, c3 = st.columns([1, 6, 2])

        with c1:
            qty = lead.get("quantity", 1) or 1
            if qty > 1:
                st.markdown(f'<div class="big-qty">{qty}×</div>', unsafe_allow_html=True)
            else:
                st.markdown('<div style="font-size:18px;color:#7a8694;">1</div>',
                            unsafe_allow_html=True)

        with c2:
            badges = f'<span class="badge {brand_class}">{brand_label}</span>'
            if lead.get("is_bulk"):
                badges += '<span class="badge badge-bulk">🔥 BULK</span>'

            safe_title = html.escape(lead["title"][:140])
            safe_url = html.escape(lead["url"], quote=True)
            st.markdown(
                f'<div class="lead-title">{badges}<a href="{safe_url}" target="_blank" rel="noopener">'
                f'{safe_title}</a></div>',
                unsafe_allow_html=True,
            )
            if lead.get("snippet"):
                snippet = lead["snippet"][:240]
                if len(lead["snippet"]) > 240: snippet += "…"
                st.markdown(
                    f'<div class="lead-meta" style="margin-top:4px;">{html.escape(snippet)}</div>',
                    unsafe_allow_html=True,
                )

            meta_parts = []
            if lead.get("price"):
                meta_parts.append(f'<span class="lead-price">{html.escape(lead["price"])}</span>')
            if lead.get("location"):
                meta_parts.append(f'📍 {html.escape(lead["location"])}')
            meta_parts.append(f'via {html.escape(lead["source"])}')
            meta_parts.append(f'first seen {format_date(lead["first_seen"])}')
            meta_parts.append(f'#{lead["id"]}')
            st.markdown(
                f'<div class="lead-meta" style="margin-top:8px;">{" · ".join(meta_parts)}</div>',
                unsafe_allow_html=True,
            )

        with c3:
            statuses = ["new", "contacted", "purchased", "dead", "ignored"]
            current = lead.get("status") or "new"
            new_status = st.selectbox(
                "Status", statuses,
                index=statuses.index(current) if current in statuses else 0,
                key=f"status_{lead['id']}", label_visibility="collapsed",
            )
            if new_status != current:
                update_listing_status(lead["id"], new_status)
                st.rerun()
            st.link_button("Open ↗", lead["url"], use_container_width=True)


# ============================================================================
# Search runner
# ============================================================================

def run_search_ui(use_google, use_ebay, use_govdeals, use_reddit, bulk_threshold):
    toro = get_setting("toro_models", DEFAULT_TORO)
    jd = get_setting("jd_models", DEFAULT_JD)
    base_queries = get_setting("base_queries", DEFAULT_BASE_QUERIES)
    subreddits = get_setting("subreddits", DEFAULT_SUBREDDITS)
    queries = build_queries(toro, jd, base_queries)

    all_results = []
    api_key = _get_secret("SERPAPI_KEY")

    with st.status(f"Searching {len(queries)} queries across selected sources…", expanded=True) as status:
        progress = st.progress(0.0)

        def cb(cur, tot, msg):
            frac = (cur + 1) / max(tot, 1)
            progress.progress(min(frac, 1.0), text=msg[:100])

        if use_google:
            if not api_key:
                status.write("⚠️ Skipping Google — no SerpAPI key configured")
            else:
                status.write(f"🔍 Searching Google for {len(queries)} queries…")
                res = search_serpapi(queries, api_key, 20, cb)
                status.write(f"  → {len(res)} raw Google results")
                all_results.extend(res)

        if use_ebay:
            status.write("🏷️ Searching eBay…")
            res = search_ebay(queries, 2, cb)
            status.write(f"  → {len(res)} raw eBay results")
            all_results.extend(res)

        if use_govdeals:
            status.write("🏛️ Searching GovDeals (municipal auctions)…")
            res = search_govdeals(queries, cb)
            status.write(f"  → {len(res)} raw GovDeals results")
            all_results.extend(res)

        if use_reddit:
            status.write("💬 Searching Reddit…")
            res = search_reddit(
                queries, subreddits,
                "mower-finder/1.0 (by usedreelmowers.com)", cb,
            )
            status.write(f"  → {len(res)} raw Reddit results")
            all_results.extend(res)

        status.write("🧮 Filtering and saving to Supabase…")
        new_count, bulk_new, relevant = enrich_and_store(all_results, bulk_threshold)

        status.update(
            label=f"✅ Done — {new_count} new listings ({bulk_new} bulk sellers)",
            state="complete", expanded=False,
        )

    return new_count, bulk_new, relevant


# ============================================================================
# Setup-required screen (shown if Supabase isn't connected yet)
# ============================================================================

db_ok, db_err = check_db_ready()

if not db_ok:
    st.title("🔍 Mower Finder — Setup needed")
    st.error(f"**Database not ready:** {db_err}")
    st.markdown("""
### Finish connecting your Supabase database

You should already have the code running on Streamlit Cloud. Two things left:

**1. Create the tables in Supabase**

Open your Supabase project → click **SQL Editor** in the left sidebar → click **New query** → paste the contents of the `schema.sql` file (in the same folder as this app) → click **Run**.

You should see "Success. No rows returned."

**2. Tell this app how to reach Supabase**

In Supabase, click the gear icon (⚙️) → **Project Settings** → **API**. You need two values:
- **Project URL** (looks like `https://xxxxx.supabase.co`)
- **service_role key** (the long secret one, NOT the anon key) — click "Reveal" to copy it

Then in Streamlit Cloud:
- Click the **⋮** menu (bottom right) → **Settings** → **Secrets**
- Paste these three lines (keep the quotes, replace with your real values):

```
SUPABASE_URL = "https://xxxxx.supabase.co"
SUPABASE_SERVICE_KEY = "eyJhbGc..."
SERPAPI_KEY = "your_serpapi_key_here_or_leave_blank"
```

- Click **Save**. The app reloads automatically.

Once both steps are done, this screen will go away and you'll see the real app.
""")
    st.stop()


# ============================================================================
# SIDEBAR
# ============================================================================

with st.sidebar:
    st.title("🔍 Mower Finder")
    st.caption("for usedreelmowers.com")
    st.divider()

    st.success("✅ Supabase connected")

    api_key = _get_secret("SERPAPI_KEY")
    if api_key:
        st.success("✅ SerpAPI key connected")
    else:
        st.warning("⚠️ No SerpAPI key — Google search disabled")
        with st.expander("How to add your key"):
            st.markdown(
                "1. Get a free key at **serpapi.com**\n"
                "2. In Streamlit Cloud: **Manage app → Settings → Secrets**\n"
                "3. Add a line: `SERPAPI_KEY = \"your_key_here\"`\n"
                "4. Save. App reloads automatically."
            )

    st.divider()
    st.subheader("Sources")
    use_google = st.checkbox("🔍 Google", value=bool(api_key), disabled=not api_key,
                             help="Broad web discovery via SerpAPI")
    use_ebay = st.checkbox("🏷️ eBay", value=True,
                           help="Public search results — great for fleet liquidations")
    use_govdeals = st.checkbox("🏛️ GovDeals", value=True,
                               help="Government surplus — municipal courses retiring fleets")
    use_reddit = st.checkbox("💬 Reddit", value=True,
                             help="r/golfcourse, r/turfgrass, etc.")

    st.divider()
    st.subheader("Detection")
    bulk_threshold = st.slider(
        "Flag as BULK if ≥ N units", 2, 10, 3,
        help="Listings meeting or exceeding this unit count get 🔥",
    )

    st.divider()
    if st.button("🚀 Run Search Now", type="primary", use_container_width=True):
        st.session_state.run_search_clicked = True

    last_search = get_setting("last_search")
    if last_search:
        st.caption(f"Last search: {format_date(last_search)}")

    with st.expander("📥 No leads yet?"):
        if st.button("Load demo data", use_container_width=True):
            new, bulk, _ = load_demo_data()
            st.success(f"Added {new} sample listings ({bulk} bulk)")
            time.sleep(1)
            st.rerun()


# ============================================================================
# MAIN AREA
# ============================================================================

if st.session_state.get("run_search_clicked"):
    st.session_state.run_search_clicked = False
    if not (use_google or use_ebay or use_govdeals or use_reddit):
        st.error("Enable at least one source in the sidebar.")
    else:
        new, bulk, relevant = run_search_ui(
            use_google, use_ebay, use_govdeals, use_reddit, bulk_threshold
        )
        if new > 0:
            st.success(f"✅ {new} new listings found, {bulk} are bulk sellers")
        else:
            st.info(
                f"No new listings this run. "
                f"{relevant} relevant listings matched but were already in your database."
            )

stats = get_stats()
c1, c2, c3, c4 = st.columns(4)
c1.metric("Total leads", stats["total"])
c2.metric("🔥 Bulk sellers", stats["bulk_total"])
c3.metric("New (unreviewed)", stats["new_count"])
c4.metric("Bulk · new", stats["bulk_new"])

st.markdown("### ")

tab_leads, tab_settings, tab_export, tab_help = st.tabs(
    ["📋 Leads", "⚙️ Settings", "💾 Export", "❓ Help"]
)


# ---- LEADS TAB ----
with tab_leads:
    if stats["total"] == 0:
        st.info(
            "👋 **Welcome!** No leads yet. Click **🚀 Run Search Now** in the sidebar, "
            "or use 'Load demo data' to see the UI with sample data."
        )
    else:
        fc1, fc2, fc3, fc4, fc5 = st.columns([1.5, 1.5, 1.5, 1.5, 2])
        bulk_only = fc1.checkbox("🔥 Bulk only", value=False)
        brand_ui = fc2.selectbox("Brand", ["All", "Toro", "John Deere", "Mixed"])
        status_filter = fc3.selectbox(
            "Status", ["All", "new", "contacted", "purchased", "dead", "ignored"]
        )
        days_filter = fc4.selectbox(
            "Recency", ["All time", "Last 7 days", "Last 30 days", "Last 90 days"]
        )
        search_text = fc5.text_input("Search title/details", placeholder="e.g. Greensmaster 1600")

        days_map = {"All time": None, "Last 7 days": 7, "Last 30 days": 30, "Last 90 days": 90}
        brand_map = {"All": None, "Toro": "toro", "John Deere": "john_deere", "Mixed": "mixed"}

        leads = fetch_listings(
            bulk_only=bulk_only,
            brand=brand_map[brand_ui],
            status=status_filter,
            days=days_map[days_filter],
            search=search_text or None,
        )

        st.caption(
            f"Showing {len(leads)} of {stats['total']} total "
            f"{'(capped at 500 — tighten filters to see older ones)' if len(leads) >= 500 else ''}"
        )

        if not leads:
            st.info("No listings match those filters.")
        else:
            for lead in leads:
                render_lead_card(lead)


# ---- SETTINGS TAB ----
with tab_settings:
    st.markdown("### Model lists & search queries")
    st.caption("Edit these to refine what the tool searches for. Saved to Supabase.")

    colA, colB = st.columns(2)
    with colA:
        st.markdown("#### Toro models")
        toro_txt = st.text_area(
            "One per line",
            "\n".join(get_setting("toro_models", DEFAULT_TORO)),
            height=260, key="toro_edit", label_visibility="collapsed",
        )
        if st.button("Save Toro models"):
            models = [l.strip() for l in toro_txt.splitlines() if l.strip()]
            set_setting("toro_models", models)
            st.success(f"Saved {len(models)} Toro models")

    with colB:
        st.markdown("#### John Deere models")
        jd_txt = st.text_area(
            "One per line",
            "\n".join(get_setting("jd_models", DEFAULT_JD)),
            height=260, key="jd_edit", label_visibility="collapsed",
        )
        if st.button("Save JD models"):
            models = [l.strip() for l in jd_txt.splitlines() if l.strip()]
            set_setting("jd_models", models)
            st.success(f"Saved {len(models)} JD models")

    st.divider()
    st.markdown("#### Broad search queries (no model names)")
    st.caption("These catch fleet liquidations and general bulk-seller language.")
    base_txt = st.text_area(
        "One per line",
        "\n".join(get_setting("base_queries", DEFAULT_BASE_QUERIES)),
        height=180, key="base_edit", label_visibility="collapsed",
    )
    if st.button("Save base queries"):
        qs = [l.strip() for l in base_txt.splitlines() if l.strip()]
        set_setting("base_queries", qs)
        st.success(f"Saved {len(qs)} base queries")

    st.divider()
    st.markdown("#### Subreddits to monitor")
    subs_txt = st.text_area(
        "One per line (no /r/ prefix)",
        "\n".join(get_setting("subreddits", DEFAULT_SUBREDDITS)),
        height=120, key="subs_edit",
    )
    if st.button("Save subreddits"):
        subs = [l.strip().lstrip("r/").lstrip("/") for l in subs_txt.splitlines() if l.strip()]
        set_setting("subreddits", subs)
        st.success(f"Saved {len(subs)} subreddits")


# ---- EXPORT TAB ----
with tab_export:
    st.markdown("### Export leads to CSV")
    st.caption(
        "Download everything as a spreadsheet. Your data is already safe in Supabase "
        "— this is for spreadsheet analysis, sharing with team, etc."
    )

    if stats["total"] > 0:
        all_rows = fetch_all_listings()
        if all_rows:
            df = pd.DataFrame(all_rows)
            cols = ["id", "source", "brand", "quantity", "is_bulk", "title", "url",
                    "price", "location", "first_seen", "status", "notes", "snippet"]
            df = df[[c for c in cols if c in df.columns]]
            csv_bytes = df.to_csv(index=False).encode()
            st.download_button(
                "📥 Download all leads as CSV", data=csv_bytes,
                file_name=f"mower_leads_{datetime.now().strftime('%Y%m%d')}.csv",
                mime="text/csv", use_container_width=True,
            )
            st.caption(f"{len(all_rows)} listings included.")
    else:
        st.info("No listings yet — run a search first.")

    st.divider()
    st.markdown("### Your data lives in Supabase")
    st.caption(
        "You can also view, edit, or query your data directly in the Supabase dashboard "
        "→ Table Editor → `mower_listings`. Good for ad-hoc SQL analysis or bulk cleanup."
    )

    st.divider()
    st.markdown("### 🚨 Danger zone")
    with st.expander("Delete all listings (cannot be undone)"):
        confirm = st.text_input('Type "DELETE" to confirm', key="confirm_delete")
        if st.button("Permanently delete all listings", type="primary"):
            if confirm == "DELETE":
                delete_all_listings()
                st.success("All listings deleted.")
                time.sleep(1)
                st.rerun()
            else:
                st.error('Type "DELETE" exactly to confirm.')


# ---- HELP TAB ----
with tab_help:
    st.markdown("""
### How this tool works

Every time you click **🚀 Run Search Now**, the app:

1. Generates ~25 targeted search queries by combining your model list with broad bulk-seller phrases
2. Runs them across the sources you've enabled (Google, eBay, GovDeals, Reddit)
3. Filters out irrelevant results (zero-turn, lawn tractors, toys, etc.)
4. Detects quantity mentions — *"fleet of 6"*, *"(4) Greensmaster"*, *"3 John Deere 220SL"*
5. Flags anything at or above your bulk threshold (default 3+ units) with 🔥
6. Saves everything to your Supabase database — dedup is automatic via URL+title fingerprinting

### Why Supabase

Your leads are now in a real Postgres database. That means:
- **Never lose data.** Even if Streamlit redeploys, everything persists.
- **Access from anywhere.** Open the Supabase dashboard → Table Editor to view, edit, or bulk-update rows directly in a spreadsheet UI.
- **Run SQL.** Want to know "how many Toro bulk leads came from GovDeals in the last 30 days by location"? You can write that query in the Supabase SQL Editor.
- **Future-proof.** Later, you could connect your Shopify store, a Zapier workflow, or any other tool directly to the same database.

### What each source is good for

- **Google (SerpAPI)** — Broadest coverage. Catches dealer sites, forum classifieds, niche auction sites. Requires a paid API key (free tier: 100 searches/mo; $50/mo for 5,000).
- **eBay** — Strong signal for fleet liquidations. Free, no key needed.
- **GovDeals** — The sleeper hit. Municipal golf courses retire fleets through government surplus auctions. Free, few of your competitors are looking here.
- **Reddit** — Low volume, high-intent posts from superintendents. Free.

### Tips

- **Run searches every few days.** Source sites don't change that quickly.
- **Work the 🔥 Bulk list first.** Highest-value leads.
- **Use the Status dropdown** to track your pipeline (new → contacted → purchased/dead).
- **Tune the model list in Settings** as you encounter new naming conventions in the wild (e.g. "GM1000" instead of "Greensmaster 1000").
- **For ad-hoc analysis**, open your Supabase dashboard → SQL Editor and write custom queries against `mower_listings`.

### Cost

- **Streamlit Cloud**: Free forever for personal/business use
- **Supabase**: Free tier covers ~50k rows in this DB, more than enough
- **SerpAPI**: Free (100 searches/mo) or $50/mo (5,000 searches ≈ 200 full scans)
- **eBay / GovDeals / Reddit**: Free
""")
