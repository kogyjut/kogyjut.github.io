import json
import re
import asyncio
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from scrapling.fetchers import StealthyFetcher

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

executor = ThreadPoolExecutor(max_workers=4)
USD_TO_AUD = 1.55

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

session = requests.Session()
session.headers.update(HEADERS)


def stealth_get(url: str) -> str:
    """Fetch a JS-rendered page bypassing anti-bot."""
    page = StealthyFetcher.fetch(url, headless=True, network_idle=True, timeout=45000)
    return page.body if hasattr(page, "body") else str(page)


# ── Grailed via Algolia ────────────────────────────────────────────────────────

GRAILED_APP_ID = "MNRWEFSS2Q"
GRAILED_INDEX = "grailed_all"


def _get_grailed_key() -> str:
    try:
        r = session.get("https://www.grailed.com", timeout=12)
        for pat in [
            r'"searchKey"\s*:\s*"([a-f0-9]{32})"',
            r'"apiKey"\s*:\s*"([a-f0-9]{32})"',
            r'ALGOLIA[^"]*"\s*,\s*"([a-f0-9]{32})"',
        ]:
            m = re.search(pat, r.text)
            if m:
                return m.group(1)
    except Exception as e:
        print(f"Key fetch error: {e}")
    return ""


def scrape_grailed(keyword: str, max_usd: float) -> list:
    api_key = _get_grailed_key()

    # Try Algolia API if we got a key
    if api_key:
        try:
            url = f"https://{GRAILED_APP_ID}-dsn.algolia.net/1/indexes/{GRAILED_INDEX}/query"
            payload = {
                "query": keyword,
                "hitsPerPage": 20,
                "numericFilters": [f"price_i<={int(max_usd)}"],
            }
            r = session.post(
                url,
                json=payload,
                headers={
                    **HEADERS,
                    "X-Algolia-Application-Id": GRAILED_APP_ID,
                    "X-Algolia-API-Key": api_key,
                },
                timeout=12,
            )
            hits = r.json().get("hits", [])
            if hits:
                return _parse_hits(hits, max_usd)
        except Exception as e:
            print(f"Algolia error: {e}")

    # Fallback — try __NEXT_DATA__ in HTML
    try:
        slug = urllib.parse.quote_plus(keyword)
        r = session.get(
            f"https://www.grailed.com/shop/listings?query={slug}",
            timeout=12,
        )
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
        if m:
            listings = (
                json.loads(m.group(1))
                .get("props", {})
                .get("pageProps", {})
                .get("listings", [])
            )
            if listings:
                return _parse_hits(listings, max_usd)
    except Exception as e:
        print(f"Grailed HTML fallback error: {e}")

    return []


def _parse_hits(hits: list, max_usd: float) -> list:
    out = []
    for h in hits:
        try:
            price = float(h.get("price_i") or h.get("price") or 0)
            if price > max_usd:
                continue
            aud = round(price * USD_TO_AUD)
            photo = h.get("cover_photo") or {}
            designers = h.get("designers") or [{}]
            brand = designers[0].get("name", "") if designers else ""
            listing_id = h.get("id") or h.get("objectID", "")
            out.append({
                "brand": brand,
                "item": (h.get("title") or "").strip(),
                "desc": (h.get("description") or "")[:140].strip(),
                "size": h.get("size") or "Check listing",
                "origPrice": f"USD {price:.0f}",
                "aud": aud,
                "under": aud < 250,
                "site": "Grailed",
                "url": f"https://www.grailed.com/listings/{listing_id}",
                "rep": "auth",
                "notes": "Live Grailed listing",
                "image": photo.get("url") or photo.get("thumb") or "",
            })
        except Exception:
            continue
    return out[:12]


# ── Depop ──────────────────────────────────────────────────────────────────────

def scrape_depop(keyword: str, max_usd: float) -> list:
    max_aud = round(max_usd * USD_TO_AUD)
    q = urllib.parse.quote_plus(keyword)
    try:
        html = stealth_get(f"https://www.depop.com/search/?q={q}")
        # Extract JSON from Next.js data
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if not m:
            return []
        data = json.loads(m.group(1))
        products = (
            data.get("props", {})
            .get("pageProps", {})
            .get("searchState", {})
            .get("products", {})
            .get("hits", [])
        )
        out = []
        for p in products:
            try:
                price_aud = float(str(p.get("price", {}).get("nationalPrice", {}).get("amountRounded", 0)))
                if price_aud > max_aud:
                    continue
                pics = p.get("pictures") or []
                slug = p.get("slug", "")
                out.append({
                    "brand": p.get("brandName") or "",
                    "item": (p.get("description") or "")[:60].strip(),
                    "desc": (p.get("description") or "")[:140].strip(),
                    "size": (p.get("sizes") or ["Check listing"])[0],
                    "origPrice": f"AUD {price_aud:.0f}",
                    "aud": round(price_aud),
                    "under": price_aud < 250,
                    "site": "Depop",
                    "url": f"https://www.depop.com/products/{slug}/",
                    "rep": "auth",
                    "notes": "Live Depop listing",
                    "image": pics[0].get("url", "") if pics else "",
                })
            except Exception:
                continue
        return out[:12]
    except Exception as e:
        print(f"Depop error: {e}")
        return []


# ── eBay AU ────────────────────────────────────────────────────────────────────

def scrape_ebay(keyword: str, max_usd: float) -> list:
    max_aud = round(max_usd * USD_TO_AUD)
    q = urllib.parse.quote_plus(keyword)
    try:
        html = stealth_get(
            f"https://www.ebay.com.au/sch/i.html"
            f"?_nkw={q}&_sacat=11450&LH_BIN=1&_udhi={max_aud}&_sop=12"
        )
        out = []
        for raw in re.findall(r'<li[^>]+s-item[^>]+>(.*?)</li>', html, re.S)[:16]:
            try:
                t = re.search(r'<span[^>]+SECONDARY_INFO[^>]*>(.*?)</span>|<h3[^>]*class="s-item__title"[^>]*>(.*?)</h3>', raw, re.S)
                title = re.sub(r'<[^>]+>', '', (t.group(1) or t.group(2) or "")).strip() if t else ""
                pm = re.search(r'\$([0-9,]+(?:\.[0-9]+)?)', raw)
                price_aud = float(pm.group(1).replace(",", "")) if pm else 0
                if not title or price_aud > max_aud or "Shop on eBay" in title:
                    continue
                lm = re.search(r'href="(https://www\.ebay\.com\.au/itm/[^"?]+)', raw)
                im = re.search(r'<img[^>]+src="([^"]+)"', raw)
                out.append({
                    "brand": "",
                    "item": title,
                    "desc": "",
                    "size": "Check listing",
                    "origPrice": f"AUD {price_aud:.0f}",
                    "aud": round(price_aud),
                    "under": price_aud < 250,
                    "site": "eBay AU",
                    "url": lm.group(1) if lm else "https://www.ebay.com.au",
                    "rep": "auth",
                    "notes": "Live eBay AU listing",
                    "image": im.group(1) if im else "",
                })
            except Exception:
                continue
        return out[:12]
    except Exception as e:
        print(f"eBay error: {e}")
        return []


# ── Routes ─────────────────────────────────────────────────────────────────────

SCRAPERS = {"grailed": scrape_grailed, "depop": scrape_depop, "ebay": scrape_ebay}


def _run(site: str, keyword: str, max_usd: float) -> list:
    fn = SCRAPERS.get(site)
    return fn(keyword, max_usd) if fn else []


@app.get("/scrape")
async def scrape(keyword: str = "", site: str = "grailed", max_price: int = 500):
    keyword = keyword.strip()
    if not keyword:
        return {"items": [], "count": 0, "error": "keyword required"}
    max_usd = round(max_price / USD_TO_AUD)
    loop = asyncio.get_event_loop()
    try:
        results = await loop.run_in_executor(executor, _run, site, keyword, max_usd)
        for i, item in enumerate(results, 1):
            item["n"] = i
        return {"items": results, "count": len(results)}
    except Exception as e:
        return {"items": [], "count": 0, "error": str(e)}


@app.get("/health")
def health():
    return {"status": "ok"}
