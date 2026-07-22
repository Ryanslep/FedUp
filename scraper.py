"""
FedUp scraping toolkit — small helper functions for the gaps the restaurant-researcher
agent's WebSearch/WebFetch tools can't cover on their own:

- render_js_page: render a JS-heavy page (SPA / ordering platform) with a headless
  browser, for sites that show a blank page or a "please enable JavaScript" wall.
- fetch_pdf_text: extract clean text from a PDF menu (WebFetch can't parse PDFs).
- foursquare_enrich: look up a business via Foursquare's free-tier search endpoint.

These are meant to be called directly, e.g. from the agent's Bash tool:
    python -c "from scraper import fetch_pdf_text; print(fetch_pdf_text('https://...'))"

All DB reads/writes happen elsewhere (get_research_batch.py / save_research.py) —
this module has no database dependency.
"""

import io
import json
import re
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from pypdf import PdfReader

# Paste your API key here to run without passing one explicitly
FOURSQUARE_API_KEY = "14SAZHJ2A3N2DLEGHH1BVYTLASYHZGVATGW1FW4WDS34QF5T"
FOURSQUARE_SEARCH_URL = "https://places-api.foursquare.com/places/search"
FOURSQUARE_API_VERSION = "2025-06-17"

SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


# ---------------------------------------------------------------------------
# Foursquare lookup
# ---------------------------------------------------------------------------

def foursquare_enrich(name: str, lat: float, lon: float, api_key: str = FOURSQUARE_API_KEY) -> dict:
    """Look up a place via Foursquare search (single call — detail endpoint requires paid tier)."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "X-Places-Api-Version": FOURSQUARE_API_VERSION,
    }
    params = {
        "query": name,
        "ll": f"{lat},{lon}",
        "radius": 200,
        "limit": 1,
    }
    try:
        resp = requests.get(FOURSQUARE_SEARCH_URL, params=params, headers=headers, timeout=10)
        if resp.status_code == 429:
            print("[foursquare] rate limit hit — skipping")
            return {}
        resp.raise_for_status()
        body = resp.json()
        if "message" in body:
            print(f"[foursquare] {body['message'][:80]}")
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
        print(f"[foursquare error] {exc}")
        return {}


# ---------------------------------------------------------------------------
# JS-rendered pages
# ---------------------------------------------------------------------------

def render_js_page(url: str) -> str | None:
    """Render a JS-heavy page (SPA / ordering platform) and return its HTML.

    Use this when WebFetch shows a blank page or a "please enable JavaScript"
    wall — plain HTTP fetches can't execute the client-side app that builds
    the real content.
    """
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                context = browser.new_context(user_agent=SCRAPE_HEADERS["User-Agent"])
                page = context.new_page()
                # Block images/fonts to speed up rendering
                page.route("**/*.{png,jpg,jpeg,gif,webp,woff,woff2,ttf}", lambda r: r.abort())
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except PlaywrightTimeout:
                    pass  # networkidle can be slow on SPAs; proceed anyway
                page.wait_for_timeout(3000)
                return page.content()
            finally:
                browser.close()
    except Exception as exc:
        print(f"[playwright error] {url}: {exc}")
        return None


# ---------------------------------------------------------------------------
# PDF menus
# ---------------------------------------------------------------------------

_UNI_GLYPH = re.compile(r"/uni([0-9A-Fa-f]{4})")


def _fix_pdf_glyphs(text: str) -> str:
    # Some embedded PDF fonts leak raw glyph names (e.g. "/uni0020") instead of
    # decoding them, when their ToUnicode CMap can't be parsed normally.
    text = _UNI_GLYPH.sub(lambda m: chr(int(m.group(1), 16)), text)
    return re.sub(r"[ \t]{2,}", " ", text)


def is_pdf_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".pdf")


def fetch_pdf_text(url: str) -> str | None:
    """Download a PDF menu and return its cleaned text (WebFetch can't parse PDFs)."""
    try:
        resp = requests.get(url, headers=SCRAPE_HEADERS, timeout=15)
        resp.raise_for_status()
        reader = PdfReader(io.BytesIO(resp.content))
        text = "\n\n".join(page.extract_text() or "" for page in reader.pages).strip()
        return _fix_pdf_glyphs(text) if text else None
    except Exception as exc:
        print(f"[pdf error] {url}: {exc}")
        return None
