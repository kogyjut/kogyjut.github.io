import json
import re
import asyncio
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from scrapling.fetchers import StealthyFetcher, Fetcher

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

executor = ThreadPoolExecutor(max_workers=3)
USD_TO_AUD = 1.55


# ── Grailed ────────────────────────────────────────────────────────────────────

def scrape_grailed(keyword: str, max_usd: float) -> list:
    slug = keyword.replace(" ", "+")
    url = f"https://www.grailed.com/shop/listings?query={slug}&sort=default"
    page = StealthyFetcher.fetch(url, headless=True, network_idle=True, timeout=45000)

    # Try Next.js server data first
    nd = page.css("script#__NEXT_DATA__")
    if nd:
        try:
            data = json.loads(nd[0].text)
            listings = data.get("props", {}).get("pageProps", {}).get("listings", [])
            if listings:
                return _parse_grailed_listings(listings, max_usd)
        except Exception:
            pass

    # Fallback CSS selectors
    for sel in [
        "article.listing-card",
        "[data-testid='listing-card']",
        "[class*='FeedItem']",
        ".feed-item",
    ]:
        cards = page.css(sel)
        if not cards:
            continue
        results = _parse_grailed_cards(cards, max_usd)
        if results:
            return results

    return []


def _parse_grailed_listings(listings: list, max_usd: float) -> list:
    out = []
    for l in listings:
        try:
            price = float(l.get("price") or 0)
            if price > max_usd:
                continue
            aud = round(price * USD_TO_AUD)
            photo = l.get("cover_photo") or {}
            out.append({
                "brand": (l.get("designer") or {}).get("name", ""),
                "item": (l.get("title") or "").strip(),
                "desc": (l.get("description") or "")[:140].strip(),
                "size": l.get("size") or "Check listing",
                "origPrice": f"USD {price:.0f}",
                "aud": aud,
                "under": aud < 250,
                "site": "Grailed",
                "url": f"https://www.grailed.com/listings/{l.get('id', '')}",
                "rep": "auth",
                "notes": "Live Grailed listing",
                "image": photo.get("url") or photo.get("thumb") or "",
            })
        except Exception:
            continue
    return out[:12]


def _parse_grailed_cards(cards, max_usd: float) -> list:
    out = []
    for card in cards:
        try:
            title_el = card.css("p, h3, h2")
            title = title_el[0].text.strip() if title_el else ""
            price_el = card.css("[class*='price']")
            price_text = price_el[0].text.strip() if price_el else "0"
            m = re.search(r"[\d.]+", price_text.replace(",", ""))
            price = float(m.group()) if m else 0
            if price > max_usd:
                continue
            link_el = card.css("a")
            href = link_el[0].attrib.get("href", "") if link_el else ""
            img_el = card.css("img")
            img = img_el[0].attrib.get("src", "") if img_el else ""
            aud = round(price * USD_TO_AUD)
            out.append({
                "brand": "",
                "item": title,
                "desc": "",
                "size": "Check listing",
                "origPrice": f"USD {price:.0f}",
                "aud": aud,
                "under": aud < 250,
                "site": "Grailed",
                "url": f"https://www.grailed.com{href}" if href.startswith("/") else href,
                "rep": "auth",
                "notes": "Live Grailed listing",
                "image": img,
            })
        except Exception:
            continue
    return out[:12]


# ── Depop ──────────────────────────────────────────────────────────────────────

def scrape_depop(keyword: str, max_usd: float) -> list:
    q = urllib.parse.quote(keyword)
    url = (
        f"https://webapi.depop.com/api/v2/search/products"
        f"?q={q}&country=au&currency=AUD&limit=24&offset=0&sort=relevance"
    )
    try:
        page = Fetcher.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                "Accept": "application/json",
            },
            timeout=20000,
        )
        data = json.loads(page.body)
        products = data.get("products", [])
        out = []
        for p in products:
            price_info = p.get("price") or {}
            try:
                price_aud = float(str(price_info.get("amount", "0")).replace(",", ""))
            except Exception:
                price_aud = 0
            if price_aud > (max_usd * USD_TO_AUD):
                continue
            pics = p.get("pictures") or []
            img = pics[0].get("url", "") if pics else ""
            sizes = p.get("sizes") or [{}]
            slug = p.get("slug", "")
            out.append({
                "brand": p.get("brandName") or "",
                "item": (p.get("description") or "")[:60].strip(),
                "desc": (p.get("description") or "")[:140].strip(),
                "size": sizes[0].get("display", "Check listing"),
                "origPrice": f"AUD {price_aud:.0f}",
                "aud": round(price_aud),
                "under": price_aud < 250,
                "site": "Depop",
                "url": f"https://www.depop.com/products/{slug}/",
                "rep": "auth",
                "notes": "Live Depop listing",
                "image": img,
            })
        return out[:12]
    except Exception as e:
        print(f"Depop error: {e}")
        return []


# ── Vestiaire Collective ───────────────────────────────────────────────────────

def scrape_vestiaire(keyword: str, max_usd: float) -> list:
    q = urllib.parse.quote(keyword)
    url = f"https://us.vestiairecollective.com/search/?q={q}"
    try:
        page = StealthyFetcher.fetch(url, headless=True, network_idle=True, timeout=45000)
        cards = page.css("[class*='product-card'], [class*='ProductCard'], article")
        out = []
        for card in cards[:12]:
            try:
                title_el = card.css("p, h2, h3, [class*='title'], [class*='name']")
                title = title_el[0].text.strip() if title_el else ""
                price_el = card.css("[class*='price'], [class*='Price']")
                price_text = price_el[0].text.strip() if price_el else "0"
                m = re.search(r"[\d,.]+", price_text.replace(",", ""))
                price_usd = float(m.group()) if m else 0
                if price_usd > max_usd:
                    continue
                link_el = card.css("a")
                href = link_el[0].attrib.get("href", "") if link_el else ""
                img_el = card.css("img")
                img = img_el[0].attrib.get("src", "") if img_el else ""
                aud = round(price_usd * USD_TO_AUD)
                out.append({
                    "brand": "",
                    "item": title,
                    "desc": "",
                    "size": "Check listing",
                    "origPrice": f"USD {price_usd:.0f}",
                    "aud": aud,
                    "under": aud < 250,
                    "site": "Vestiaire Collective",
                    "url": href if href.startswith("http") else f"https://us.vestiairecollective.com{href}",
                    "rep": "auth",
                    "notes": "Live Vestiaire listing — authenticated resale",
                    "image": img,
                })
            except Exception:
                continue
        return out
    except Exception as e:
        print(f"Vestiaire error: {e}")
        return []


# ── Routes ─────────────────────────────────────────────────────────────────────

SCRAPERS = {
    "grailed": scrape_grailed,
    "depop": scrape_depop,
    "vestiaire": scrape_vestiaire,
}


def _run(site: str, keyword: str, max_usd: float) -> list:
    fn = SCRAPERS.get(site)
    if not fn:
        return []
    return fn(keyword, max_usd)


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
