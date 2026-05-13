import json
import re
import uuid
import asyncio
import urllib.parse
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import requests
from fastapi import FastAPI, Body
from fastapi.middleware.cors import CORSMiddleware
from scrapling.fetchers import StealthyFetcher

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

WALL_FILE = Path("wall.json")

def _load_wall():
    if WALL_FILE.exists():
        try:
            return json.loads(WALL_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"posts": []}

def _save_wall(data):
    WALL_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

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
    """Fetch a JS-rendered page bypassing anti-bot, return HTML string."""
    page = StealthyFetcher.fetch(url, headless=True, network_idle=True, timeout=45000)
    body = page.body if hasattr(page, "body") else b""
    return body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)


def stealth_page(url: str):
    """Fetch and return the Scrapling page object (has .css() selector support)."""
    return StealthyFetcher.fetch(url, headless=True, network_idle=True, timeout=45000)


# ── Grailed via Algolia ────────────────────────────────────────────────────────

GRAILED_APP_ID = "MNRWEFSS2Q"
GRAILED_INDEX = "grailed_all"


def scrape_grailed(keyword: str, max_usd: float) -> list:
    slug = urllib.parse.quote_plus(keyword)
    try:
        page = stealth_page(f"https://www.grailed.com/shop?query={slug}")
    except Exception as e:
        print(f"Grailed stealth fetch error: {e}")
        return []

    listing_links = page.css('a[href*="/listings/"]')
    if not listing_links:
        return []

    out = []
    for a in listing_links:
        try:
            href = a.attrib.get("href", "")
            id_match = re.search(r'/listings/(\d+)', href)
            if not id_match:
                continue
            lid = id_match.group(1)

            slug_match = re.search(r'/listings/\d+-(.+?)(?:\?|$)', href)
            title = slug_match.group(1).replace("-", " ").title() if slug_match else keyword.title()

            # Walk up to the card container that has a price span
            node = a
            price_usd = 0.0
            img_url = ""
            for _ in range(8):
                if node is None:
                    break
                price_spans = [
                    s.text for s in (node.css("span") if hasattr(node, "css") else [])
                    if s.text and "$" in s.text and len(s.text) < 20
                ]
                if price_spans:
                    pm = re.search(r'[\d,]+(?:\.\d+)?', price_spans[0].replace(",", ""))
                    price_usd = float(pm.group()) if pm else 0.0
                    imgs = node.css("img")
                    if imgs:
                        # Use srcset for best quality; strip trailing ? and append width
                        raw = imgs[0].attrib.get("src", "").rstrip("?")
                        img_url = raw + "?w=640" if raw and "?" not in raw else raw
                    break
                node = node.parent if hasattr(node, "parent") else None

            if not price_usd or price_usd > max_usd:
                continue

            aud = round(price_usd * USD_TO_AUD)
            out.append({
                "brand": keyword.title(),
                "item": title,
                "desc": "",
                "size": "Check listing",
                "origPrice": f"USD {price_usd:.0f}",
                "aud": aud,
                "under": aud < 250,
                "site": "Grailed",
                "url": f"https://www.grailed.com/listings/{lid}",
                "rep": "auth",
                "notes": "Live Grailed listing",
                "image": img_url,
            })
        except Exception:
            continue

    return out[:12]


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
        page = stealth_page(f"https://www.depop.com/search/?q={q}")
    except Exception as e:
        print(f"Depop fetch error: {e}")
        return []

    product_links = page.css('a[href*="/products/"]')
    if not product_links:
        return []

    out = []
    seen = set()
    for a in product_links:
        try:
            href = a.attrib.get("href", "")
            if not href or href in seen:
                continue
            seen.add(href)

            slug = href.strip("/").split("/")[-1]
            # Slug format: username-product-name-HEXID — strip username and trailing hex ID
            parts = slug.split("-")
            # Drop first segment (username) and last segment if it looks like a hex ID
            inner = parts[1:] if len(parts) > 1 else parts
            if inner and re.fullmatch(r'[0-9a-fA-F]{4,}', inner[-1]):
                inner = inner[:-1]
            title = " ".join(inner).title() if inner else keyword.title()

            # Walk up to card container with price
            node = a
            price_aud = 0.0
            img_url = ""
            for _ in range(8):
                if node is None:
                    break
                price_els = [
                    s.text for s in (node.css("p,span") if hasattr(node, "css") else [])
                    if s.text and "$" in s.text and len(s.text) < 15
                ]
                if price_els:
                    pm = re.search(r'[\d.]+', price_els[0].replace(",", ""))
                    price_aud = float(pm.group()) if pm else 0.0
                    imgs = node.css("img")
                    if imgs:
                        # Prefer _mainImage over _blurImage placeholder
                        main = next((i for i in imgs if "_mainImage" in (i.attrib.get("class") or "")), imgs[0])
                        img_url = main.attrib.get("src", "")
                    break
                node = node.parent if hasattr(node, "parent") else None

            if not price_aud or price_aud > max_aud:
                continue

            out.append({
                "brand": "",
                "item": title,
                "desc": "",
                "size": "Check listing",
                "origPrice": f"AUD {price_aud:.0f}",
                "aud": round(price_aud),
                "under": price_aud < 250,
                "site": "Depop",
                "url": f"https://www.depop.com{href}",
                "rep": "auth",
                "notes": "Live Depop listing",
                "image": img_url,
            })
        except Exception:
            continue

    return out[:12]


# ── Vinted AU ──────────────────────────────────────────────────────────────────

def scrape_vinted(keyword: str, max_usd: float) -> list:
    max_aud = round(max_usd * USD_TO_AUD)
    q = urllib.parse.quote_plus(keyword)
    try:
        page = stealth_page(f"https://www.vinted.com.au/catalog?search_text={q}")
    except Exception as e:
        print(f"Vinted fetch error: {e}")
        return []

    product_links = page.css('a[href*="/items/"]')
    if not product_links:
        return []

    out = []
    seen = set()
    for a in product_links:
        try:
            href = a.attrib.get("href", "")
            if not href or href in seen:
                continue
            seen.add(href)

            node = a
            price_aud = 0.0
            img_url = ""
            title = ""
            for _ in range(8):
                if node is None:
                    break
                price_els = [
                    s.text for s in (node.css("p,span") if hasattr(node, "css") else [])
                    if s.text and "$" in s.text and len(s.text) < 15
                ]
                if price_els:
                    pm = re.search(r'[\d.]+', price_els[0].replace(",", ""))
                    price_aud = float(pm.group()) if pm else 0.0
                    imgs = node.css("img")
                    if imgs:
                        img_url = imgs[0].attrib.get("src", "")
                    title_els = [s.text for s in node.css("p,span,h3")
                                 if s.text and "$" not in s.text and len(s.text) > 3 and len(s.text) < 80]
                    title = title_els[0] if title_els else keyword.title()
                    break
                node = node.parent if hasattr(node, "parent") else None

            if not price_aud or price_aud > max_aud:
                continue

            out.append({
                "brand": "",
                "item": title or keyword.title(),
                "desc": "",
                "size": "Check listing",
                "origPrice": f"AUD {price_aud:.0f}",
                "aud": round(price_aud),
                "under": price_aud < 250,
                "site": "Vinted",
                "url": f"https://www.vinted.com.au{href}" if href.startswith("/") else href,
                "rep": "auth",
                "notes": "Live Vinted listing",
                "image": img_url,
            })
        except Exception:
            continue

    return out[:12]


# ── Routes ─────────────────────────────────────────────────────────────────────

SCRAPERS = {"grailed": scrape_grailed, "depop": scrape_depop, "vinted": scrape_vinted}


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


@app.get("/posts")
def get_posts():
    return _load_wall()

@app.post("/post")
def add_post(item: dict = Body(...)):
    wall = _load_wall()
    post = {
        "id": uuid.uuid4().hex[:8],
        "posted_at": datetime.now().isoformat(),
        "item": item,
        "comments": [],
    }
    wall["posts"].insert(0, post)
    wall["posts"] = wall["posts"][:100]
    _save_wall(wall)
    return {"ok": True, "post": post}

@app.post("/comment")
def add_comment(body: dict = Body(...)):
    post_id = body.get("post_id", "")
    text = (body.get("text") or "").strip()[:280]
    name = (body.get("name") or "Bro").strip()[:32] or "Bro"
    if not post_id or not text:
        return {"ok": False, "error": "missing fields"}
    wall = _load_wall()
    for post in wall["posts"]:
        if post["id"] == post_id:
            post["comments"].append({
                "id": uuid.uuid4().hex[:8],
                "name": name,
                "text": text,
                "posted_at": datetime.now().isoformat(),
            })
            _save_wall(wall)
            return {"ok": True}
    return {"ok": False, "error": "post not found"}

@app.get("/health")
def health():
    return {"status": "ok"}
