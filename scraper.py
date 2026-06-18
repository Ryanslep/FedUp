"""
FedUp scraper — enriches the places table with data from Yelp and restaurant websites.

Usage:
    python scraper.py --yelp-key YOUR_API_KEY [--limit N] [--place-id ID]

Adds columns to places: yelp_id, phone, website, menu_url, hours, rating, review_count,
                         deals_text, scraped_menu_text, last_scraped

Playwright is used automatically for pages that require JavaScript (ordering platforms,
SPAs, etc.). Falls back to plain requests for simple HTML pages.
"""

import argparse
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import urlparse, unquote, urlencode, parse_qs

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

DB_PATH = "fedup.db"

# Paste your API keys here to run without --yelp-key / --foursquare-key flags
YELP_API_KEY = ""
FOURSQUARE_API_KEY = "14SAZHJ2A3N2DLEGHH1BVYTLASYHZGVATGW1FW4WDS34QF5T"
YELP_SEARCH_URL = "https://api.yelp.com/v3/businesses/search"
YELP_DETAIL_URL = "https://api.yelp.com/v3/businesses/{}"

SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

SKIP_DOMAINS = {
    "yelp.com", "tripadvisor.com", "grubhub.com", "doordash.com",
    "ubereats.com", "seamless.com", "opentable.com", "facebook.com",
    "instagram.com", "twitter.com", "google.com", "duckduckgo.com",
    "wikipedia.org", "bing.com", "yellowpages.com", "mapquest.com",
}

# Ordering-platform domains that always need JS rendering
JS_REQUIRED_DOMAINS = {
    "incentivio.com", "toasttab.com", "olo.com", "squareup.com",
    "order.online", "bopple.com", "bentobox.com", "popmenu.com",
    "restolabs.com", "menudrive.com", "owner.com", "chownow.com",
}

MENU_PATTERN = re.compile(r"\bmenu\b", re.I)

DEALS_PATTERNS = [
    re.compile(r"(happy hour[^<]{0,200})", re.I | re.S),
    re.compile(r"(daily deal[^<]{0,200})", re.I | re.S),
    re.compile(r"(today'?s? special[^<]{0,200})", re.I | re.S),
    re.compile(r"(\$\d+(?:\.\d+)? off[^<]{0,100})", re.I | re.S),
]


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def ensure_columns(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    existing = {row[1] for row in cur.execute("PRAGMA table_info(places)")}
    new_cols = {
        "yelp_id": "TEXT",
        "phone": "TEXT",
        "website": "TEXT",
        "menu_url": "TEXT",
        "hours": "TEXT",
        "rating": "REAL",
        "review_count": "INTEGER",
        "deals_text": "TEXT",
        "scraped_menu_text": "TEXT",
        "last_scraped": "TEXT",
    }
    for col, dtype in new_cols.items():
        if col not in existing:
            cur.execute(f"ALTER TABLE places ADD COLUMN {col} {dtype}")
    conn.commit()


def get_places(conn: sqlite3.Connection, limit: int | None, place_id: int | None):
    cur = conn.cursor()
    if place_id:
        cur.execute("SELECT id, name, lat, lon, website FROM places WHERE id = ?", (place_id,))
    else:
        query = "SELECT id, name, lat, lon, website FROM places ORDER BY id"
        if limit:
            query += f" LIMIT {limit}"
        cur.execute(query)
    return cur.fetchall()


def save_place(conn: sqlite3.Connection, place_id: int, data: dict) -> None:
    data["last_scraped"] = datetime.now(timezone.utc).isoformat()
    cols = ", ".join(f"{k} = ?" for k in data)
    vals = list(data.values()) + [place_id]
    conn.execute(f"UPDATE places SET {cols} WHERE id = ?", vals)
    conn.commit()


# ---------------------------------------------------------------------------
# Yelp
# ---------------------------------------------------------------------------

def yelp_search(name: str, lat: float, lon: float, api_key: str) -> dict | None:
    params = {
        "term": name,
        "latitude": lat,
        "longitude": lon,
        "limit": 1,
        "radius": 200,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        resp = requests.get(YELP_SEARCH_URL, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        businesses = resp.json().get("businesses", [])
        return businesses[0] if businesses else None
    except requests.RequestException as exc:
        print(f"    [yelp search error] {exc}")
        return None


def yelp_details(yelp_id: str, api_key: str) -> dict | None:
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        resp = requests.get(YELP_DETAIL_URL.format(yelp_id), headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        print(f"    [yelp detail error] {exc}")
        return None


def extract_yelp_data(biz: dict) -> dict:
    hours_open = biz.get("hours", [])
    hours_str = json.dumps(hours_open[0].get("open", [])) if hours_open else None
    return {
        "yelp_id": biz.get("id"),
        "phone": biz.get("display_phone") or biz.get("phone"),
        "website": biz.get("url"),
        "menu_url": biz.get("menu_url"),
        "rating": biz.get("rating"),
        "review_count": biz.get("review_count"),
        "hours": hours_str,
    }


# ---------------------------------------------------------------------------
# Website discovery
# ---------------------------------------------------------------------------

FOURSQUARE_SEARCH_URL = "https://places-api.foursquare.com/places/search"
FOURSQUARE_DETAIL_URL = "https://places-api.foursquare.com/places/{}"
FOURSQUARE_API_VERSION = "2025-06-17"


def foursquare_enrich(name: str, lat: float, lon: float, api_key: str) -> dict:
    """Look up a place via Foursquare search (single call — detail endpoint requires paid tier)."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "X-Places-Api-Version": FOURSQUARE_API_VERSION,
    }
    # Request all fields available in the free search endpoint
    params = {
        "query": name,
        "ll": f"{lat},{lon}",
        "radius": 200,
        "limit": 1,
    }
    try:
        resp = requests.get(FOURSQUARE_SEARCH_URL, params=params, headers=headers, timeout=10)
        if resp.status_code == 429:
            print(f"    [foursquare] rate limit hit — skipping")
            return {}
        resp.raise_for_status()
        body = resp.json()
        if "message" in body:
            print(f"    [foursquare] {body['message'][:80]}")
            return {}
        results = body.get("results", [])
        if not results:
            return {}
        place = results[0]
        hours_str = None
        if "hours" in place:
            hours_str = json.dumps(place["hours"].get("regular", place["hours"]))
        return {k: v for k, v in {
            "website": place.get("website"),
            "phone": place.get("tel"),
            "hours": hours_str,
            "rating": place.get("rating"),
            "menu_url": place.get("menu"),
        }.items() if v}
    except requests.RequestException as exc:
        print(f"    [foursquare error] {exc}")
        return {}


def guess_website(name: str) -> str | None:
    """Try common URL patterns for a restaurant name when no API is available."""
    import re as _re
    slug = _re.sub(r"[^a-z0-9]", "", name.lower())
    candidates = [
        f"https://www.{slug}.com",
        f"https://{slug}.com",
        f"https://www.{slug}charlotte.com",
    ]
    for url in candidates:
        try:
            resp = requests.get(url, headers=SCRAPE_HEADERS, timeout=6, allow_redirects=True)
            if resp.status_code < 400:
                return url
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# HTML fetching — plain requests + Playwright fallback
# ---------------------------------------------------------------------------

def _needs_js(url: str) -> bool:
    domain = urlparse(url).netloc.lstrip("www.")
    return any(js in domain for js in JS_REQUIRED_DOMAINS)


def _fetch_html_requests(url: str) -> str | None:
    try:
        resp = requests.get(url, headers=SCRAPE_HEADERS, timeout=12, allow_redirects=True)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as exc:
        print(f"    [fetch error] {exc}")
        return None


def _fetch_html_playwright(url: str, page) -> str | None:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=25000)
        # Wait for network to settle, then extra time for JS frameworks to render
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except PlaywrightTimeout:
            pass  # networkidle can be slow on SPAs; proceed anyway
        page.wait_for_timeout(3000)
        return page.content()
    except PlaywrightTimeout:
        print(f"    [playwright timeout] {url}")
        return None
    except Exception as exc:
        print(f"    [playwright error] {exc}")
        return None


def fetch_html(url: str, page=None) -> str | None:
    if _needs_js(url) and page:
        print(f"    [playwright] rendering {url}")
        html = _fetch_html_playwright(url, page)
        if html and "enable JavaScript" not in html:
            return html
    # Plain requests (also used as fallback if playwright returns JS-wall content)
    html = _fetch_html_requests(url)
    # If the page still shows a JS wall, try playwright as fallback
    if html and "enable JavaScript" in html and page:
        print(f"    [playwright fallback] {url}")
        html = _fetch_html_playwright(url, page)
    return html


# ---------------------------------------------------------------------------
# Content extraction
# ---------------------------------------------------------------------------

def _find_menu_link(soup: BeautifulSoup, base_url: str) -> str | None:
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        href = a["href"]
        if MENU_PATTERN.search(text) or MENU_PATTERN.search(href):
            if href.startswith("http"):
                return href
            elif href.startswith("/"):
                parts = urlparse(base_url)
                return f"{parts.scheme}://{parts.netloc}{href}"
    return None


def _extract_deals(soup: BeautifulSoup) -> str | None:
    text = soup.get_text(separator=" ", strip=True)
    hits = []
    for pat in DEALS_PATTERNS:
        for m in pat.finditer(text):
            hits.append(m.group(0).strip()[:300])
    return "\n---\n".join(hits) if hits else None


def _clean_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)


def scrape_website(website_url: str, page=None) -> dict:
    result: dict = {}

    html = fetch_html(website_url, page)
    if not html:
        return result

    soup = BeautifulSoup(html, "html.parser")

    # Deals from homepage
    deals = _extract_deals(soup)
    if deals:
        result["deals_text"] = deals

    # Follow menu link
    menu_link = _find_menu_link(soup, website_url)
    if menu_link and menu_link != website_url:
        menu_html = fetch_html(menu_link, page)
        if menu_html:
            menu_soup = BeautifulSoup(menu_html, "html.parser")
            menu_text = _clean_text(menu_soup)
            if menu_text and "enable JavaScript" not in menu_text:
                result["scraped_menu_text"] = menu_text[:4000]
                result["menu_url"] = menu_link
            elif page:
                # Already tried playwright inside fetch_html — nothing more to do
                result["menu_url"] = menu_link

    return result


# ---------------------------------------------------------------------------
# Main enrichment loop
# ---------------------------------------------------------------------------

def enrich_place(
    place_id: int,
    name: str,
    lat: float,
    lon: float,
    existing_website: str | None,
    yelp_key: str | None,
    foursquare_key: str | None,
    conn: sqlite3.Connection,
    page=None,
) -> None:
    print(f"  {name} (id={place_id})")
    data: dict = {}

    # --- Yelp ---
    if yelp_key:
        biz = yelp_search(name, lat, lon, yelp_key)
        if biz:
            detail = yelp_details(biz["id"], yelp_key)
            if detail:
                data.update(extract_yelp_data(detail))
                print(f"    [yelp] found: {detail.get('url', '')}")
            else:
                data.update(extract_yelp_data(biz))
        else:
            print("    [yelp] no match")

    # --- Foursquare (alternative to Yelp) ---
    if foursquare_key and not data.get("website"):
        fsq_data = foursquare_enrich(name, lat, lon, foursquare_key)
        if fsq_data:
            data.update({k: v for k, v in fsq_data.items() if v})
            print(f"    [foursquare] found: {fsq_data.get('website', '')}")
        else:
            print("    [foursquare] no match")

    # --- Website discovery ---
    # Use cached DB value, then fall back to URL pattern guessing
    website = data.get("website")
    bad_website = not website or any(d in (website or "") for d in ["yelp.com", "bing.com"])

    if bad_website and existing_website and not any(d in existing_website for d in ["yelp.com", "bing.com"]):
        website = existing_website
        data["website"] = website
        bad_website = False

    if bad_website:
        guessed = guess_website(name)
        if guessed:
            print(f"    [guess] found: {guessed}")
            website = guessed
            data["website"] = website

    # --- Website scraping ---
    website = data.get("website")
    if website and not any(d in website for d in ["yelp.com", "bing.com"]):
        print(f"    [scrape] {website}")
        scraped = scrape_website(website, page)
        if "menu_url" in scraped:
            data["menu_url"] = scraped.pop("menu_url")
        data.update(scraped)
        time.sleep(1)

    if data:
        save_place(conn, place_id, data)
        print(f"    [saved] {list(data.keys())}")
    else:
        print("    [no data]")


def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich FedUp places with menu/deals data")
    parser.add_argument("--yelp-key", default=YELP_API_KEY or None, help="Yelp Fusion API key (recommended)")
    parser.add_argument("--foursquare-key", default=FOURSQUARE_API_KEY or None, help="Foursquare Places API key (free alternative to Yelp)")
    parser.add_argument("--limit", type=int, help="Max number of places to process")
    parser.add_argument("--place-id", type=int, help="Process a single place by ID")
    parser.add_argument("--no-playwright", action="store_true", help="Disable Playwright (faster, but misses JS-rendered menus)")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    ensure_columns(conn)

    places = get_places(conn, args.limit, args.place_id)
    print(f"Processing {len(places)} place(s)...\n")

    if args.no_playwright:
        for place_id, name, lat, lon, existing_website in places:
            try:
                enrich_place(place_id, name, lat, lon, existing_website,
                             args.yelp_key, args.foursquare_key, conn, page=None)
            except Exception as exc:
                print(f"    [error] {exc}")
            time.sleep(2)
    else:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(user_agent=SCRAPE_HEADERS["User-Agent"])
            page = context.new_page()
            # Block images/fonts to speed up JS rendering
            page.route(
                "**/*.{png,jpg,jpeg,gif,webp,woff,woff2,ttf}",
                lambda r: r.abort()
            )
            try:
                for place_id, name, lat, lon, existing_website in places:
                    try:
                        enrich_place(place_id, name, lat, lon, existing_website,
                                     args.yelp_key, args.foursquare_key, conn, page=page)
                    except Exception as exc:
                        print(f"    [error] {exc}")
                    time.sleep(2)
            finally:
                browser.close()

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
