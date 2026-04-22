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
    .badge-star { background: #ffc107; color: #000; }
    .badge-type { background: #2a3240; color: #9aa7b5; border: 1px solid #3a4250; }
    .big-qty { font-size: 32px; font-weight: 800; color: #ff6b35; line-height: 1; }
    .lead-title a { color: #e4e7eb; text-decoration: none; font-weight: 600; }
    .lead-title a:hover { color: #00c389; }
    .lead-meta { color: #7a8694; font-size: 13px; }
    .lead-price { color: #00c389; font-weight: 600; }
    div[data-testid="stMetricValue"] { color: #00c389; }
    .category-header {
        font-size: 18px; font-weight: 700; color: #e4e7eb;
        padding: 16px 0 8px 0; border-bottom: 1px solid #2a3240;
        margin: 24px 0 12px 0;
    }
    .category-header .count {
        color: #7a8694; font-weight: 400; font-size: 14px; margin-left: 8px;
    }
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

# Queries specifically designed to find pages on individual golf course websites
# that list used equipment for sale. Use Google dorks like "site:" and intitle:"
# to force Google to look for the pattern we want.
DEFAULT_GOLF_COURSE_QUERIES = [
    '"used equipment" "for sale" golf course "greensmaster" OR "e-cut"',
    '"pro shop" OR "superintendent" "for sale" greensmaster walking',
    '"golf club" OR "country club" classifieds "greens mower"',
    'intitle:"equipment for sale" golf course greensmaster',
    'intitle:"used equipment" golf "walking greens"',
    '"golf course" "for sale" "John Deere 220" OR "John Deere 180SL"',
    '"golf course" "fleet" "Toro Greensmaster" selling',
    'golf course "equipment sales" greensmaster walk behind',
    '"superintendent" selling "walking greens mower" -reddit',
    '"country club" "surplus equipment" greens mower',
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
        # Toys / models / RC
        r"\btoy\b|\bmodel\s*car\b|\b(?:rc|radio\s*control)\b|\bminiature\b|"
        # Zero-turn / riding consumer mowers
        r"z[-\s]?trak|\bzero[-\s]*turn\b|\briding\s*mower\b|\bpush\s*mower\b|"
        r"\brotary\s*mower\b|\blawn\s*tractor\b|"
        # Toro riding triplex / triflex greens mowers (NOT walking)
        r"\btriplex\b|\btriflex\b|\btri[-\s]?plex\b|\btri[-\s]?flex\b|"
        # Specific Toro riding Greensmaster models (3100, 3150, 3200, 3250, 3300, 3320, 3400, 3420, 3500)
        r"\bgreensmaster\s*3\d{3}\b|\b3[0-5]\d{2}\s*(?:greensmaster|reel|triplex|triflex|riding)\b|"
        # Standalone 3300 / 3320 / 3400 etc. when preceded by Toro/greensmaster nearby
        r"\btoro\s*3[0-5]\d{2}\b|"
        # John Deere riding triplex models (2500, 2550, 2653, 2700, 2750, 7200, 7500, 8500, 8700, 8800, 8900)
        r"\b(?:2500|2550|2653|2700|2750|7200|7500|8500|8700|8800|8900)[a-z]?\s*(?:greens|john|jd|deere|triplex|riding|precisioncut)\b|"
        r"\b(?:john\s*deere|jd|deere)\s*(?:2500|2550|2653|2700|2750|7200|7500|8500|8700|8800|8900)\b|"
        # Fairway mowers (different beast entirely)
        r"\bfairway\s*mower\b|\bfairway\s*reel\b|"
        # Rough mowers
        r"\brough\s*mower\b"
        , text))
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
# URL quality filter: reject generic search pages, category pages, etc. that
# would just take the user to a list of results instead of a specific listing
# ============================================================================

def url_is_specific_listing(url):
    """
    Return False if a URL looks like a search results page, category page,
    or other generic landing page rather than a specific item listing.
    Return True if it looks like (or could plausibly be) a specific listing.
    """
    if not url:
        return False
    u = url.lower()

    # eBay: valid item URLs contain /itm/; anything under /sch/ (search),
    # /b/ (browse/category), /e/ (events), /str/ (stores), etc. is generic
    if "ebay.com" in u:
        if "/itm/" in u:
            return True
        # Anything on ebay.com that's NOT an item URL is a search/category/etc.
        return False

    # Facebook Marketplace: valid item URLs contain /marketplace/item/<id>
    # Everything else (/marketplace/{city}/..., /marketplace/category/...,
    # /marketplace/search/?query=...) is a search/browse/category page
    if "facebook.com" in u:
        if "/marketplace/item/" in u:
            return True
        if "/marketplace/" in u:
            return False
        # Facebook group posts are usable - superintendents post in private groups
        if re.search(r"/groups/[^/]+/posts/\d+", u):
            return True
        if re.search(r"/groups/[^/]+/permalink/\d+", u):
            return True
        # Bare group landing or marketplace root - reject
        if re.search(r"/marketplace/?$|/groups/[^/]+/?$", u):
            return False
        return True  # other FB URLs (pages, posts): give benefit of the doubt

    # Craigslist: item URLs have a 10+ digit post ID and end in .html
    if "craigslist.org" in u:
        if re.search(r"/\d{10,}\.html", u):
            return True
        if "/search/" in u or "?query=" in u:
            return False

    # OfferUp: item URLs are /item/detail/<id>
    if "offerup.com" in u:
        if "/item/detail/" in u:
            return True
        return False

    # Google: shouldn't hit this since we use SerpAPI, but guard anyway
    if u.startswith("https://www.google.com/search") or u.startswith("https://google.com/search"):
        return False

    # Generic search-page indicators anywhere in the path
    search_page_patterns = [
        r"/search/?(?:\?|$)",
        r"/sch/",
        r"/category/",
        r"/categories/",
        r"/listings/?(?:\?|$)",
        r"/browse/?(?:\?|$)",
        r"/shop/?(?:\?|$)",
        r"[?&]q=.+&",
        r"/results/?(?:\?|$)",
    ]
    for pat in search_page_patterns:
        if re.search(pat, u):
            # Only allow through if there's a specific ID-like segment AFTER the category
            # path, indicating drill-down to a specific item
            if not re.search(
                r"(?:/category/|/categories/|/search/|/sch/|/browse/|/shop/|/listings/|/results/)[^/]+/(?:\d{6,}|[a-z0-9\-]{15,})(?:/|$|\?|\.html)",
                u,
            ):
                return False

    # GovDeals: valid item URLs have fa=Main.Item
    if "govdeals.com" in u and "fa=main.item" not in u and "/asset/" not in u:
        # But category pages are fa=Main.AdvSearchResultsNew - reject
        if "fa=main.advsearch" in u or "fa=main.category" in u:
            return False

    return True


# ============================================================================
# Listing type classifier (auction / dealer / marketplace / classified / other)
# ============================================================================

# Known auction-type domains and phrases
AUCTION_DOMAINS = {
    "govdeals.com", "publicsurplus.com", "bidspotter.com", "proxibid.com",
    "hibid.com", "liveauctioneers.com", "auctionzip.com", "equipmentfacts.com",
    "ironplanet.com", "ritchiebros.com", "rbauction.com", "allsurplus.com",
    "municibid.com", "purplewave.com",
}
AUCTION_TITLE_RX = re.compile(
    r"\bauction\b|\bbid(?:ding)?\b|\bestate\s*sale\b|\bsurplus\b|"
    r"\bsheriff['s]*\s*sale\b|\bliquidation\s*auction\b", re.I,
)

# Marketplace-style domains (open listings, fixed-price or best-offer)
MARKETPLACE_DOMAINS = {
    "ebay.com", "ebay.co.uk", "facebook.com", "craigslist.org",
    "offerup.com", "mercari.com", "letgo.com", "shopgoodwill.com",
    "equipmenttrader.com", "machinerytrader.com", "tractorhouse.com",
    "golfequipmenttrader.com",
}

# Dealer/reseller indicators in titles or domain structure
DEALER_TITLE_RX = re.compile(
    r"\b(dealer|dealership|authorized\s*(?:toro|john\s*deere)|turf\s*(?:equipment|supply)|"
    r"golf\s*(?:equipment|supply)|outdoor\s*power\s*(?:equipment|supply)|"
    r"used\s*equipment\s*sales|reconditioned|refurbished|inventory|"
    r"our\s*(?:fleet|inventory|stock))\b",
    re.I,
)

# Forum / classified / community post indicators
CLASSIFIED_DOMAINS = {
    "reddit.com", "turfnetwork.com", "turfnet.com", "golfwrx.com",
    "lawnsite.com", "plowsite.com", "tractorbynet.com", "mytractorforum.com",
    "golfclub-atlas.com",
}


def _domain_of(url):
    try:
        from urllib.parse import urlparse
        netloc = urlparse(url).netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc
    except Exception:
        return ""


def classify_listing_type(url, title, snippet, source, seller=None):
    """Return one of: 'golf_course', 'auction', 'dealer', 'marketplace', 'classified', 'other'."""
    domain = _domain_of(url)
    text = " ".join(filter(None, [title, snippet]))

    # Golf course direct - check FIRST since these are the highest-value leads.
    # Three signals, in order of reliability:
    #   1. Seller name contains "Country Club", "Golf Club", "Golf Course", etc.
    #      (from TurfNet) - THIS IS THE STRONGEST SIGNAL
    #   2. Domain is an individual golf course website
    #   3. Title/snippet mentions a specific golf course selling equipment
    if seller and _seller_is_golf_course(seller):
        return "golf_course"
    if _looks_like_golf_course_domain(domain):
        return "golf_course"

    # TurfNet listings - if not already classified as golf_course via seller,
    # they're treated as classified (still valuable but not course-direct)
    if source == "turfnet":
        return "classified"

    # Source-based shortcuts (high confidence)
    if source == "govdeals":
        return "auction"
    if source and source.startswith("reddit/"):
        return "classified"
    if source == "ebay":
        return "marketplace"

    # Domain-based classification (very reliable)
    if any(d in domain for d in AUCTION_DOMAINS):
        return "auction"
    if any(d in domain for d in MARKETPLACE_DOMAINS):
        return "marketplace"
    if any(d in domain for d in CLASSIFIED_DOMAINS):
        return "classified"

    # Text-based classification (less reliable, but useful for unknown domains)
    if AUCTION_TITLE_RX.search(text):
        return "auction"
    if DEALER_TITLE_RX.search(text):
        return "dealer"

    # Heuristic: domains with "dealer", "turf", "equipment", "supply" in them
    if re.search(r"dealer|turf|equipment|supply|golf[\w-]*pro|mower", domain, re.I):
        return "dealer"

    return "other"


def _seller_is_golf_course(seller):
    """Detect if a seller/poster name indicates an individual golf course/country club."""
    if not seller:
        return False
    s = seller.lower()
    # Patterns that indicate a specific course, NOT an equipment dealer
    course_indicators = [
        r"\bcountry\s*club\b",
        r"\bgolf\s*club\b",
        r"\bgolf\s*course\b",
        r"\bgolf\s*resort\b",
        r"\blinks\s*(?:at|of)\b",
        r"\bg\.?\s*c\.?\b(?!\s*turf)",  # "GC" but not "GC Turf"
        r"\bc\.?\s*c\.?\b(?!\s*turf)",  # "CC" but not "CC Turf"
        # "X Golf" as the ending (e.g. "Rio Pinar Golf", "Pebble Beach Golf")
        # — but only if no other commercial term preceded
        r"^(?:[A-Z][a-zA-Z]+\s+){1,3}Golf\s*$",
    ]
    # Skip if it's clearly an equipment dealer/reseller
    dealer_excludes = [
        r"turf\s*equipment",
        r"turf\s*supply",
        r"reel\s*(?:sharp|mowers|services)",
        r"\bllc\b.*\b(?:equipment|supply|mowers|turf|reel)\b",
        r"\b(?:equipment|supply|mowers|turf|reel)\b.*\bllc\b",
        r"statewide\s*turf",
        r"general\s*turf",
        r"cutting\s*green",
        r"western\s*turf",
    ]
    for pat in dealer_excludes:
        if re.search(pat, s, re.I):
            return False
    for pat in course_indicators:
        if re.search(pat, s, re.I):
            return True
    return False


def _looks_like_golf_course_domain(domain):
    """Detect golf course / country club websites by domain pattern."""
    if not domain:
        return False

    # Exclude the big aggregator/industry sites that have 'golf' in their name
    # but aren't individual courses
    BIG_GOLF_SITES = {
        "golfwrx.com", "golfdigest.com", "golf.com", "pga.com", "pgatour.com",
        "golfchannel.com", "mygolfspy.com", "golfclub-atlas.com", "golfweek.com",
        "usga.org", "gcsaa.org", "golfcourseindustry.com", "golfbusiness.com",
        "golfequipmenttrader.com",
    }
    if domain in BIG_GOLF_SITES:
        return False

    # Subdomains of those big sites also exclude
    for big in BIG_GOLF_SITES:
        if domain.endswith("." + big):
            return False

    # Strip TLD to get just the main domain part for substring matching
    # e.g. "pinevalleygolfclub.com" -> "pinevalleygolfclub"
    # e.g. "shop.oakvalleycc.com" -> "oakvalleycc"
    main = domain.split(".")
    if len(main) >= 2:
        main_name = main[-2]  # second-to-last segment before TLD
    else:
        main_name = domain

    # Token-based match for hyphen/dot separated domains
    tokens = re.split(r"[.\-_]", domain)
    if any(t in {"golf", "cc", "countryclub", "golfclub", "links", "golfcourse", "gc"}
           for t in tokens):
        return True

    # Substring match in the main domain name for compound words:
    # "pinevalleygolfclub", "torreypinesgolfcourse", "bandondunesgolf",
    # "mygolfclub", "somecountryclub", etc.
    if re.search(
        r"golfclub|countryclub|golfcourse|golflinks|golf$|cc$",
        main_name, re.I,
    ):
        return True

    # Common TLD suffixes - .gc.com, .cc.com etc. (though rare)
    if re.search(r"gc\.(?:com|org|net)$|cc\.(?:com|org|net)$", domain):
        return True

    return False


LISTING_TYPE_LABELS = {
    "golf_course": "⛳ Golf Course Direct",
    "auction":     "🔨 Auction",
    "dealer":      "🏪 Dealer / Reseller",
    "marketplace": "🛒 Marketplace",
    "classified":  "💬 Classified / Forum",
    "other":       "📰 Other",
}

LISTING_TYPE_ORDER = ["golf_course", "auction", "dealer", "marketplace", "classified", "other"]


# ============================================================================
# Model extraction (for filtering)
# ============================================================================

# Pre-compiled per-model regexes. The idea: extract which *specific model* a
# listing is about, so the user can filter by it. Order matters — more specific
# patterns (e.g. "Flex 1820") must come before general ones (e.g. "Greensmaster").
MODEL_PATTERNS = [
    # Toro specific models
    ("Toro Greensmaster Flex 1820",    re.compile(r"greensmaster\s*flex\s*1820|\bflex\s*1820\b", re.I)),
    ("Toro Greensmaster Flex 2120",    re.compile(r"greensmaster\s*flex\s*2120|\bflex\s*2120\b", re.I)),
    ("Toro Greensmaster Flex 21",      re.compile(r"greensmaster\s*flex\s*21\b|\bflex\s*21\b", re.I)),
    ("Toro Greensmaster eFlex 1021",   re.compile(r"eflex\s*1021\b", re.I)),
    ("Toro Greensmaster eFlex 1800",   re.compile(r"eflex\s*1800\b", re.I)),
    ("Toro Greensmaster eFlex 2100",   re.compile(r"eflex\s*2100\b", re.I)),
    ("Toro Greensmaster eFlex 2120",   re.compile(r"eflex\s*2120\b", re.I)),
    ("Toro Greensmaster eFlex",        re.compile(r"\beflex\b", re.I)),
    ("Toro Greensmaster 1000",         re.compile(r"greensmaster\s*1000\b|\bgm\s*1000\b", re.I)),
    ("Toro Greensmaster 1600",         re.compile(r"greensmaster\s*1600\b|\bgm\s*1600\b", re.I)),
    ("Toro Greensmaster 800",          re.compile(r"greensmaster\s*800\b|\bgm\s*800\b", re.I)),
    ("Toro Greensmaster (other)",      re.compile(r"greensmaster|\btoro\b.*\bgreens\b", re.I)),
    # John Deere specific models
    ("JD 220 E-Cut",                   re.compile(r"\b220\s*e[-\s]?cut\b", re.I)),
    ("JD 180 E-Cut",                   re.compile(r"\b180\s*e[-\s]?cut\b", re.I)),
    ("JD 220SL",                       re.compile(r"\b220\s*sl\b", re.I)),
    ("JD 260SL",                       re.compile(r"\b260\s*sl\b", re.I)),
    ("JD 180SL",                       re.compile(r"\b180\s*sl\b", re.I)),
    ("JD PrecisionCut 180",            re.compile(r"precisioncut\s*180\b", re.I)),
    ("JD PrecisionCut 220",            re.compile(r"precisioncut\s*220\b", re.I)),
    ("JD PrecisionCut 260",            re.compile(r"precisioncut\s*260\b", re.I)),
    ("JD PrecisionCut (other)",        re.compile(r"precisioncut", re.I)),
    ("JD E-Cut (other)",               re.compile(r"e[-\s]?cut", re.I)),
    ("JD (other)",                     re.compile(r"john\s*deere|\bjd\b|\bdeere\b", re.I)),
]


def detect_model(*texts):
    blob = " ".join(t for t in texts if t)
    for name, pat in MODEL_PATTERNS:
        if pat.search(blob):
            return name
    return "Unknown model"


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


def search_turfnet(max_pages=3, exclude_own_listings=True, progress_cb=None):
    """
    Scrape TurfNet's Walk Greensmower classifieds category (in_cat=631).
    These are direct-from-superintendent and direct-from-dealer listings,
    and the seller name tells us if it's a golf course directly.

    Publicly accessible - no login needed.
    """
    out = []
    base = "https://turfnet.com/classifieds/"

    for page in range(1, max_pages + 1):
        if progress_cb: progress_cb(page - 1, max_pages, f"TurfNet walk-greensmower page {page}")
        try:
            if page == 1:
                url = f"{base}?in_cat=631&directory_type=equipment"
            else:
                url = f"{base}page/{page}/?directory_type=equipment&in_cat=631"

            r = requests.get(url, headers=HEADERS, timeout=25)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")

            # Each listing is an H2 with a link to /directory/equipment/<slug>/
            listing_links = soup.select('h2 a[href*="/directory/equipment/"]')
            if not listing_links:
                # Fallback selector
                listing_links = soup.select('a[href*="/directory/equipment/"]')

            seen_urls_this_page = set()
            for link in listing_links:
                href = link.get("href", "")
                if not href or href in seen_urls_this_page:
                    continue
                seen_urls_this_page.add(href)

                # Normalize URL
                if href.startswith("/"):
                    href = "https://turfnet.com" + href

                title = link.get_text(" ", strip=True)
                if not title or len(title) < 5:
                    continue

                # Find the parent container to extract price, seller, location
                container = link.find_parent(["article", "div", "li"])
                price = None
                location = None
                seller = None
                if container:
                    # Price is usually a dollar amount near the title
                    price_match = re.search(r"\$[\d,]+(?:\.\d{2})?", container.get_text())
                    if price_match:
                        price = price_match.group(0)

                    # Location and seller appear after the title in list items
                    # Typical format: $X.00 Location Name, State Seller Name count
                    text_blob = container.get_text(" ", strip=True)
                    # Try to extract a "City, State" pattern
                    loc_match = re.search(
                        r"([A-Z][a-zA-Z\s]+),\s*(Alabama|Alaska|Arizona|Arkansas|California|"
                        r"Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|Illinois|"
                        r"Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|"
                        r"Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|"
                        r"New Hampshire|New Jersey|New Mexico|New York|North Carolina|"
                        r"North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|"
                        r"South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|"
                        r"Washington|West Virginia|Wisconsin|Wyoming)",
                        text_blob,
                    )
                    if loc_match:
                        location = f"{loc_match.group(1).strip()}, {loc_match.group(2)}"

                    # Seller name: look for company-like strings after the location
                    # e.g. "General Turf Equipment LLC", "Bryn Mawr Country Club", "usedreelmowers.com"
                    # These tend to appear right after the location in a paragraph
                    for p_el in container.select("p, .listing-author, .author, .seller"):
                        p_text = p_el.get_text(" ", strip=True)
                        # Skip if it's the price/location line
                        if "$" in p_text and len(p_text) < 100:
                            continue
                        # Look for common seller patterns
                        for line in p_text.split("\n"):
                            line = line.strip()
                            if re.search(
                                r"(?:LLC|Inc|Country\s*Club|Golf\s*Club|Golf\s*Course|"
                                r"Turf|Equipment|Greens|Reels?|usedreelmowers)",
                                line, re.I,
                            ):
                                seller = line[:80]
                                break
                        if seller:
                            break

                # Skip user's own listings
                if exclude_own_listings and seller and "usedreelmowers" in seller.lower():
                    continue
                if exclude_own_listings and "usedreelmowers" in title.lower():
                    continue

                # Build snippet from seller + title
                snippet_parts = []
                if seller: snippet_parts.append(f"Seller: {seller}")
                snippet = " · ".join(snippet_parts)

                out.append({
                    "source": "turfnet",
                    "title": title,
                    "url": href.split("?")[0].split("#")[0],
                    "snippet": snippet,
                    "price": price,
                    "location": location,
                    "seller": seller,  # used by classifier
                    "query": "turfnet walk greensmower",
                })

            time.sleep(1.5)
        except Exception as e:
            if progress_cb: progress_cb(page - 1, max_pages, f"TurfNet error p{page}: {e}")
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
        if not url_is_specific_listing(r.get("url", "")):
            continue
        if not location_acceptable(r.get("title", ""), r.get("location", "")):
            continue
        r["brand"] = detect_brand(*texts)
        if r["brand"] == "unknown": continue
        r["quantity"] = extract_quantity(*texts)
        has_hint = bool(BULK_HINT_RX.search(" ".join(texts).lower()))
        r["is_bulk"] = r["quantity"] >= bulk_threshold or has_hint
        r["listing_type"] = classify_listing_type(
            r.get("url", ""), r.get("title", ""), r.get("snippet", ""),
            r.get("source", ""), r.get("seller"),
        )
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
            "listing_type": r.get("listing_type", "other"),
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
        # Auction-type
        {"source": "govdeals", "title": "3 John Deere 220 E-Cut Walking Greens Mowers - Municipal Golf Course",
         "url": "https://govdeals.com/itm/2222", "snippet": "Three JD 220 E-Cut mowers from city golf course. Auction ends Friday.",
         "price": "$4,200", "location": "Dayton, OH", "query": "John Deere 220 E-Cut"},
        {"source": "google", "title": "Estate sale auction: Complete fleet of Toro Greensmaster 1600",
         "url": "https://bidspotter.com/auction/4567", "snippet": "Estate auction of closed golf course, 6 Greensmaster 1600 units, all serviced.",
         "price": "Starting bid $8,000", "location": "Scottsdale, AZ", "query": "golf course auction"},
        # Dealer-type
        {"source": "google", "title": "Used Toro Greensmaster 1000 - Reconditioned | Turf Equipment USA",
         "url": "https://turfequipmentusa.com/inventory/toro-1000",
         "snippet": "Our inventory includes 4 reconditioned Greensmaster 1000 units. Authorized Toro dealer.",
         "price": "$6,500 each", "location": "Dallas, TX", "query": "Toro Greensmaster 1000"},
        {"source": "google", "title": "John Deere 220SL Walk Greens Mower | SouthEast Turf Supply",
         "url": "https://southeastturfsupply.com/listings/jd-220sl",
         "snippet": "Single JD 220SL in excellent shape, low hours. Authorized JD dealer.",
         "price": "$5,800", "location": "Charleston, SC", "query": "John Deere 220SL"},
        # Marketplace-type
        {"source": "ebay", "title": "(4) Toro Greensmaster Flex 1820 Walk Behind Reel Greens Mower",
         "url": "https://ebay.com/itm/1111", "snippet": "4 units available, all functional",
         "price": "$8,500", "location": "Orlando, FL", "query": "Toro Greensmaster Flex 1820"},
        {"source": "ebay", "title": "Toro Greensmaster eFlex 1021 Lithium Battery Walking Greens Mower",
         "url": "https://ebay.com/itm/3333", "snippet": "Modern electric walking greens mower, single unit",
         "price": "$9,900", "location": "Dallas, TX", "query": "Toro Greensmaster eFlex"},
        # Classified/forum-type
        {"source": "reddit/golfcourse", "title": "Anyone know a buyer for two Toro Greensmaster 1000s?",
         "url": "https://reddit.com/r/golfcourse/p3", "snippet": "We have 2 Greensmaster 1000 units retiring this spring",
         "price": None, "location": None, "query": "Toro Greensmaster 1000"},
        {"source": "reddit/turfgrass", "title": "Superintendent selling fleet of 5 JD PrecisionCut 180",
         "url": "https://reddit.com/r/turfgrass/p4", "snippet": "Retiring 5 PrecisionCut 180 units this fall, reasonable offers considered.",
         "price": None, "location": "Austin, TX", "query": "John Deere PrecisionCut 180"},
    ]
    return enrich_and_store(demos, bulk_threshold=3)


# ============================================================================
# Data access (Supabase reads)
# ============================================================================

def fetch_listings(bulk_only=False, starred_only=False, brand=None, status=None,
                   listing_type=None, source=None, days=None, search=None,
                   sort="smart", limit=1000):
    """
    Query listings with rich filters and sort options.

    sort:
      - "smart"    : starred first, bulk first, then newest (default)
      - "newest"   : first_seen desc
      - "oldest"   : first_seen asc
      - "quantity" : quantity desc, then newest
      - "bulk"     : bulk first, then quantity desc, then newest
    """
    supa = get_supabase()
    if supa is None:
        return []
    q = supa.table(LISTINGS_TABLE).select("*")

    if bulk_only:
        q = q.eq("is_bulk", True)
    if starred_only:
        q = q.eq("starred", True)
    if brand and brand not in ("All", None):
        q = q.eq("brand", brand)
    if status and status not in ("All", None):
        q = q.eq("status", status)
    if listing_type and listing_type not in ("All", None):
        # Match by auto-detected type (override column compared client-side)
        q = q.eq("listing_type", listing_type)
    if source and source not in ("All", None):
        q = q.eq("source", source)
    if days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        q = q.gte("first_seen", cutoff)
    if search:
        safe = search.replace(",", " ").replace("(", " ").replace(")", " ")
        q = q.or_(f"title.ilike.%{safe}%,snippet.ilike.%{safe}%")

    # Sort
    if sort == "newest":
        q = q.order("first_seen", desc=True)
    elif sort == "oldest":
        q = q.order("first_seen", desc=False)
    elif sort == "quantity":
        q = q.order("quantity", desc=True).order("first_seen", desc=True)
    elif sort == "bulk":
        q = q.order("is_bulk", desc=True).order("quantity", desc=True).order("first_seen", desc=True)
    else:  # smart
        q = (q.order("starred", desc=True)
              .order("is_bulk", desc=True)
              .order("first_seen", desc=True))

    q = q.limit(limit)
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
        "starred":    count(t.select("id", count="exact", head=True).eq("starred", True)),
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


def toggle_star(listing_id, starred):
    supa = get_supabase()
    if supa is None:
        return
    try:
        supa.table(LISTINGS_TABLE).update({"starred": bool(starred)}).eq("id", listing_id).execute()
    except Exception as e:
        st.error(f"Star update failed: {e}")


def delete_listing(listing_id):
    supa = get_supabase()
    if supa is None:
        return
    try:
        supa.table(LISTINGS_TABLE).delete().eq("id", listing_id).execute()
    except Exception as e:
        st.error(f"Delete failed: {e}")


def update_listing_notes(listing_id, notes):
    supa = get_supabase()
    if supa is None:
        return
    try:
        supa.table(LISTINGS_TABLE).update({"notes": notes}).eq("id", listing_id).execute()
    except Exception as e:
        st.error(f"Notes save failed: {e}")


def override_listing_type(listing_id, listing_type):
    supa = get_supabase()
    if supa is None:
        return
    try:
        supa.table(LISTINGS_TABLE).update(
            {"listing_type_override": listing_type}
        ).eq("id", listing_id).execute()
    except Exception as e:
        st.error(f"Type override failed: {e}")


def get_effective_type(lead):
    """Return the user override if set, otherwise the auto-detected type."""
    return lead.get("listing_type_override") or lead.get("listing_type") or "other"


def get_distinct_sources():
    """Return sorted list of distinct source values actually in the DB."""
    supa = get_supabase()
    if supa is None:
        return []
    try:
        resp = supa.table(LISTINGS_TABLE).select("source").limit(5000).execute()
        vals = sorted({r["source"] for r in (resp.data or []) if r.get("source")})
        return vals
    except Exception:
        return []


def get_distinct_models():
    """Return sorted list of models actually present, based on running the
    extractor over every listing's title+snippet. Done in Python so we don't
    need a stored model column."""
    supa = get_supabase()
    if supa is None:
        return []
    try:
        resp = supa.table(LISTINGS_TABLE).select("title,snippet").limit(5000).execute()
        models = set()
        for r in (resp.data or []):
            models.add(detect_model(r.get("title", ""), r.get("snippet", "") or ""))
        return sorted(m for m in models if m != "Unknown model") + (
            ["Unknown model"] if "Unknown model" in models else []
        )
    except Exception:
        return []


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
    lid = lead["id"]

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
            # Row of badges: brand, bulk, type, starred
            badges = f'<span class="badge {brand_class}">{brand_label}</span>'
            if lead.get("is_bulk"):
                badges += '<span class="badge badge-bulk">🔥 BULK</span>'
            type_label = LISTING_TYPE_LABELS.get(get_effective_type(lead), "📰 Other")
            badges += f'<span class="badge badge-type">{html.escape(type_label)}</span>'
            if lead.get("starred"):
                badges += '<span class="badge badge-star">⭐ STARRED</span>'

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

            # Show the domain prominently so user can triage at a glance
            domain = _domain_of(lead.get("url", ""))
            if domain:
                # Truncate the URL path for readability
                url_display = lead["url"]
                if len(url_display) > 90:
                    url_display = url_display[:87] + "…"
                st.markdown(
                    f'<div class="lead-meta" style="margin-top:6px;">'
                    f'🔗 <span style="color:#9aa7b5;font-weight:500;">{html.escape(domain)}</span>'
                    f' <span style="color:#5a6470;font-size:12px;">{html.escape(url_display)}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

            meta_parts = []
            if lead.get("price"):
                meta_parts.append(f'<span class="lead-price">{html.escape(lead["price"])}</span>')
            if lead.get("location"):
                meta_parts.append(f'📍 {html.escape(lead["location"])}')
            meta_parts.append(f'via {html.escape(lead["source"])}')
            meta_parts.append(f'first seen {format_date(lead["first_seen"])}')
            meta_parts.append(f'#{lid}')
            st.markdown(
                f'<div class="lead-meta" style="margin-top:8px;">{" · ".join(meta_parts)}</div>',
                unsafe_allow_html=True,
            )

            # Inline notes - collapsed by default unless there's already a note
            existing_notes = lead.get("notes") or ""
            notes_label = "📝 Edit notes" if existing_notes else "📝 Add notes"
            with st.expander(notes_label, expanded=bool(existing_notes)):
                new_notes = st.text_area(
                    "Notes",
                    value=existing_notes,
                    key=f"notes_{lid}",
                    label_visibility="collapsed",
                    placeholder="e.g. called seller, left VM, waiting callback",
                    height=80,
                )
                if new_notes != existing_notes:
                    if st.button("Save notes", key=f"save_notes_{lid}", type="primary"):
                        update_listing_notes(lid, new_notes)
                        st.rerun()

        with c3:
            # Status dropdown
            statuses = ["new", "contacted", "purchased", "dead", "ignored"]
            current = lead.get("status") or "new"
            new_status = st.selectbox(
                "Status", statuses,
                index=statuses.index(current) if current in statuses else 0,
                key=f"status_{lid}", label_visibility="collapsed",
            )
            if new_status != current:
                update_listing_status(lid, new_status)
                st.rerun()

            # Open link
            st.link_button("Open ↗", lead["url"], use_container_width=True)

            # Star + Delete row
            starred = bool(lead.get("starred"))
            sb, db = st.columns(2)
            if sb.button(
                "⭐" if not starred else "★ starred",
                key=f"star_{lid}",
                use_container_width=True,
                help="Star this lead to keep it at the top",
            ):
                toggle_star(lid, not starred)
                st.rerun()
            if db.button(
                "🗑️",
                key=f"del_{lid}",
                use_container_width=True,
                help="Delete this listing (can't be undone)",
            ):
                st.session_state[f"confirm_del_{lid}"] = True

            if st.session_state.get(f"confirm_del_{lid}"):
                st.warning(f"Delete #{lid}?", icon="⚠️")
                cc1, cc2 = st.columns(2)
                if cc1.button("Yes, delete", key=f"yesdel_{lid}", type="primary", use_container_width=True):
                    delete_listing(lid)
                    st.session_state.pop(f"confirm_del_{lid}", None)
                    st.rerun()
                if cc2.button("Cancel", key=f"nodel_{lid}", use_container_width=True):
                    st.session_state.pop(f"confirm_del_{lid}", None)
                    st.rerun()


# ============================================================================
# Search runner
# ============================================================================

def run_search_ui(use_google, use_ebay, use_govdeals, use_reddit, use_turfnet, bulk_threshold):
    toro = get_setting("toro_models", DEFAULT_TORO)
    jd = get_setting("jd_models", DEFAULT_JD)
    base_queries = get_setting("base_queries", DEFAULT_BASE_QUERIES)
    golf_queries = get_setting("golf_course_queries", DEFAULT_GOLF_COURSE_QUERIES)
    subreddits = get_setting("subreddits", DEFAULT_SUBREDDITS)
    queries = build_queries(toro, jd, base_queries)

    all_results = []
    api_key = _get_secret("SERPAPI_KEY")

    total_google = len(queries) + (len(golf_queries) if use_google and api_key else 0)
    status_msg = f"Searching across selected sources…"
    with st.status(status_msg, expanded=True) as status:
        progress = st.progress(0.0)

        def cb(cur, tot, msg):
            frac = (cur + 1) / max(tot, 1)
            progress.progress(min(frac, 1.0), text=msg[:100])

        if use_google:
            if not api_key:
                status.write("⚠️ Skipping Google — no SerpAPI key configured")
            else:
                status.write(f"🔍 Searching Google for {len(queries)} standard queries…")
                res = search_serpapi(queries, api_key, 20, cb)
                status.write(f"  → {len(res)} raw Google results")
                all_results.extend(res)

                # Extra pass: golf-course-targeted Google queries
                if golf_queries:
                    status.write(f"⛳ Searching Google for {len(golf_queries)} golf-course-targeted queries…")
                    res_golf = search_serpapi(golf_queries, api_key, 20, cb)
                    status.write(f"  → {len(res_golf)} raw golf-course results")
                    all_results.extend(res_golf)

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

        if use_turfnet:
            status.write("⛳ Searching TurfNet Walk Greensmower classifieds…")
            res = search_turfnet(max_pages=3, exclude_own_listings=True, progress_cb=cb)
            status.write(f"  → {len(res)} raw TurfNet results")
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
    use_turfnet = st.checkbox("⛳ TurfNet Classifieds", value=True,
                              help="Direct-from-golf-course and direct-from-dealer listings. "
                                   "The Walk Greensmower category is public and doesn't need login.")

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
    if not (use_google or use_ebay or use_govdeals or use_reddit or use_turfnet):
        st.error("Enable at least one source in the sidebar.")
    else:
        new, bulk, relevant = run_search_ui(
            use_google, use_ebay, use_govdeals, use_reddit, use_turfnet, bulk_threshold
        )
        if new > 0:
            st.success(f"✅ {new} new listings found, {bulk} are bulk sellers")
        else:
            st.info(
                f"No new listings this run. "
                f"{relevant} relevant listings matched but were already in your database."
            )

stats = get_stats()
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Total leads", stats["total"])
c2.metric("🔥 Bulk sellers", stats["bulk_total"])
c3.metric("⭐ Starred", stats["starred"])
c4.metric("New (unreviewed)", stats["new_count"])
c5.metric("Bulk · new", stats["bulk_new"])

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
        # Top toggle row: view mode + quick filters
        tc1, tc2, tc3, tc4 = st.columns([2, 1.2, 1.2, 1.2])
        view_mode = tc1.radio(
            "View", ["Grouped by type", "Flat list"], horizontal=True,
            label_visibility="collapsed",
        )
        bulk_only = tc2.checkbox("🔥 Bulk only")
        starred_only = tc3.checkbox("⭐ Starred only")
        hide_ignored = tc4.checkbox("Hide ignored", value=True)

        # Filter row 1
        fc1, fc2, fc3, fc4 = st.columns(4)
        brand_ui = fc1.selectbox("Brand", ["All", "Toro", "John Deere", "Mixed"])
        type_options = ["All"] + LISTING_TYPE_ORDER
        type_labels_ui = ["All"] + [LISTING_TYPE_LABELS[t] for t in LISTING_TYPE_ORDER]
        type_choice = fc2.selectbox("Listing type", type_labels_ui)
        # Map label back to db value
        if type_choice == "All":
            type_filter_db = None
        else:
            type_filter_db = LISTING_TYPE_ORDER[type_labels_ui.index(type_choice) - 1]

        status_filter = fc3.selectbox(
            "Status", ["All", "new", "contacted", "purchased", "dead", "ignored"]
        )
        days_filter = fc4.selectbox(
            "Recency", ["All time", "Last 7 days", "Last 30 days", "Last 90 days"]
        )

        # Filter row 2
        gc1, gc2, gc3, gc4 = st.columns(4)
        source_options = ["All"] + get_distinct_sources()
        source_filter = gc1.selectbox("Source", source_options)

        model_options = ["All"] + get_distinct_models()
        model_filter = gc2.selectbox("Model", model_options)

        sort_labels = {
            "smart": "⭐ Smart (starred, bulk, newest)",
            "newest": "🕐 Newest first",
            "oldest": "🕐 Oldest first",
            "quantity": "🔢 Most units first",
            "bulk": "🔥 Bulk first, then qty",
        }
        sort_choice = gc3.selectbox(
            "Sort", list(sort_labels.keys()),
            format_func=lambda k: sort_labels[k],
        )

        search_text = gc4.text_input("Search title/details",
                                     placeholder="e.g. Greensmaster 1600")

        days_map = {"All time": None, "Last 7 days": 7,
                    "Last 30 days": 30, "Last 90 days": 90}
        brand_map = {"All": None, "Toro": "toro",
                     "John Deere": "john_deere", "Mixed": "mixed"}

        leads = fetch_listings(
            bulk_only=bulk_only,
            starred_only=starred_only,
            brand=brand_map[brand_ui],
            status=None if status_filter == "All" else status_filter,
            listing_type=type_filter_db,
            source=None if source_filter == "All" else source_filter,
            days=days_map[days_filter],
            search=search_text or None,
            sort=sort_choice,
            limit=1000,
        )

        # Client-side: hide ignored, filter by model (model isn't a stored column)
        if hide_ignored and status_filter == "All":
            leads = [l for l in leads if l.get("status") != "ignored"]

        if model_filter != "All":
            leads = [
                l for l in leads
                if detect_model(l.get("title", ""), l.get("snippet", "") or "") == model_filter
            ]

        # Apply type override client-side (so user manual overrides take effect
        # for display AND for grouping)
        if type_filter_db is not None:
            leads = [l for l in leads if get_effective_type(l) == type_filter_db]

        # Summary line
        summary_bits = [f"Showing **{len(leads)}** of {stats['total']} total"]
        if leads:
            by_type = {}
            for l in leads:
                t = get_effective_type(l)
                by_type[t] = by_type.get(t, 0) + 1
            breakdown = " · ".join(
                f"{LISTING_TYPE_LABELS[t]}: {by_type[t]}"
                for t in LISTING_TYPE_ORDER if t in by_type
            )
            summary_bits.append(breakdown)
        st.markdown(" &nbsp; • &nbsp; ".join(summary_bits))

        if len(leads) >= 1000:
            st.caption("Result set capped at 1000. Tighten filters to see more.")

        if not leads:
            st.info("No listings match those filters. Try clearing some above.")
        else:
            if view_mode == "Flat list":
                for lead in leads:
                    render_lead_card(lead)
            else:
                # Grouped view — render each listing type as its own section
                grouped = {t: [] for t in LISTING_TYPE_ORDER}
                for lead in leads:
                    grouped.setdefault(get_effective_type(lead), []).append(lead)

                for t in LISTING_TYPE_ORDER:
                    bucket = grouped.get(t, [])
                    if not bucket:
                        continue
                    st.markdown(
                        f'<div class="category-header">{LISTING_TYPE_LABELS[t]}'
                        f'<span class="count">· {len(bucket)} listing'
                        f'{"s" if len(bucket) != 1 else ""}</span></div>',
                        unsafe_allow_html=True,
                    )
                    for lead in bucket:
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
    st.markdown("#### ⛳ Golf course targeted queries (Google only)")
    st.caption(
        "These queries use Google dorks (site:, intitle:, quoted phrases) to find "
        "classified sections on individual golf course websites. "
        "**Only runs when Google source is enabled.** Each query = 1 SerpAPI search."
    )
    golf_txt = st.text_area(
        "One per line",
        "\n".join(get_setting("golf_course_queries", DEFAULT_GOLF_COURSE_QUERIES)),
        height=200, key="golf_edit", label_visibility="collapsed",
    )
    if st.button("Save golf course queries"):
        qs = [l.strip() for l in golf_txt.splitlines() if l.strip()]
        set_setting("golf_course_queries", qs)
        st.success(f"Saved {len(qs)} golf course queries")

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
            cols = ["id", "source", "listing_type", "listing_type_override", "brand",
                    "quantity", "is_bulk", "starred", "title", "url",
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
    st.markdown("### 🧹 Bulk cleanup")
    st.caption(
        "Quickly remove listings that are no longer useful. "
        "Less destructive than the danger zone below."
    )
    cc1, cc2 = st.columns([2, 1])
    cleanup_status = cc1.selectbox(
        "Delete listings with status",
        ["ignored", "dead", "ignored + dead"],
        help="Frees up the list by removing leads you've already dismissed.",
    )
    if cc2.button("Run cleanup", use_container_width=True):
        supa = get_supabase()
        if supa is not None:
            try:
                if cleanup_status == "ignored + dead":
                    supa.table(LISTINGS_TABLE).delete().in_("status", ["ignored", "dead"]).execute()
                else:
                    supa.table(LISTINGS_TABLE).delete().eq("status", cleanup_status).execute()
                st.success(f"Cleaned up listings with status: {cleanup_status}")
                time.sleep(1)
                st.rerun()
            except Exception as e:
                st.error(f"Cleanup failed: {e}")

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
4. Restricts to continental US (excludes Hawaii, Alaska, non-US)
5. Detects quantity mentions — *"fleet of 6"*, *"(4) Greensmaster"*, *"3 John Deere 220SL"*
6. Classifies each listing into a type: Auction / Dealer / Marketplace / Classified / Other
7. Flags bulk-seller listings (default: 3+ units) with 🔥
8. Saves everything to your Supabase database — deduped automatically

### Managing leads

Each lead card has:
- **⭐ Star** — pin important leads to the top in Smart sort
- **🗑️ Delete** — permanently remove a listing (asks for confirmation)
- **📝 Notes** — jot notes like "called seller, waiting callback"
- **Status** dropdown — move leads through your pipeline: new → contacted → purchased / dead / ignored
- **Open ↗** — go to the original listing

### Filters and views

- **Grouped view** (default) — organizes listings by type: Auction, Dealer, Marketplace, Classified, Other
- **Flat list** — single feed, useful when you want everything sorted together
- **Filters**: Brand, Listing type, Status, Recency, Source, Model, + free-text search
- **Sort**: Smart (starred → bulk → newest) / Newest / Oldest / Most units / Bulk first
- **Hide ignored** — on by default, keeps dismissed leads out of sight

### What each listing type means

- 🔨 **Auction** — GovDeals, estate sales, timed auctions with deadlines. Act fast.
- 🏪 **Dealer/Reseller** — professional turf equipment dealers. Best for negotiating bulk pricing.
- 🛒 **Marketplace** — eBay, Craigslist-style. Usually fixed-price or best-offer.
- 💬 **Classified/Forum** — Reddit, TurfNet, GolfWRX. Direct-from-superintendent signals, often unlisted elsewhere.
- 📰 **Other** — anything that didn't fit above (press releases, blog posts, unknown sellers).

### Why Supabase

Your leads are in a real Postgres database. That means:
- **Never lose data.** Even if Streamlit redeploys, everything persists.
- **Access from anywhere.** Supabase dashboard → Table Editor for a spreadsheet UI view of every lead.
- **Run SQL.** Write custom queries like *"top 10 cities for bulk Toro leads in the last 90 days"*.
- **Future-proof.** Later, connect Zapier, email automations, or your Shopify store to the same database.

### What each source is good for

- **Google (SerpAPI)** — Broadest coverage. Dealer sites, forum classifieds, niche auctions. Requires SerpAPI key.
- **eBay** — Strong for fleet liquidations. Free, no key needed.
- **GovDeals** — The sleeper hit. Municipal golf courses retire fleets via gov surplus auctions. Free.
- **Reddit** — Low volume, high-intent superintendent posts. Free.

### Tips

- **Run searches every few days.** Sources don't change that quickly.
- **Work the 🔥 Bulk list first, then ⭐ starred.** Highest-value leads.
- **Use 🗑️ delete for obvious junk, "Status → ignored" for maybes.** Ignored leads are hidden by default but you can still pull them back; deleted ones are gone.
- **Add notes as you reach out.** Future you will thank present you.
- **Tune the model list in Settings** as you encounter new naming conventions in the wild.

### Cost

- **Streamlit Cloud**: Free forever
- **Supabase**: Free tier covers ~50k listings, way more than you need
- **SerpAPI**: Free (100 searches/mo) or $50/mo (5,000 searches ≈ 200 full scans)
- **eBay / GovDeals / Reddit**: Free
""")
