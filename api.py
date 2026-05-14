import json
import re
import uuid
import asyncio
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests as _requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Body
from fastapi.middleware.cors import CORSMiddleware
from playwright.async_api import async_playwright

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

WALL_FILE = Path("wall.json")
USD_TO_AUD = 1.55
JPY_PER_AUD = 98  # 1 AUD ≈ 98 yen

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

BROWSER_ARGS = [
    "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
    "--disable-gpu", "--no-first-run", "--no-zygote", "--single-process",
]

# ── eBay keyword filter (only used there to cut noise) ─────────────────────────

_STOP = {"a","an","the","and","or","for","in","on","at","to","of","is","it","its","with","by","from","s"}

def _kw_words(keyword: str) -> list:
    return [w for w in keyword.lower().split() if w not in _STOP and len(w) >= 3]

def _relevant(title: str, kw_words: list) -> bool:
    if not kw_words:
        return True
    t = title.lower()
    return any(w in t for w in kw_words)


def _load_wall():
    if WALL_FILE.exists():
        try:
            return json.loads(WALL_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"posts": []}


def _save_wall(data):
    WALL_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Grailed — Algolia API (no Playwright, much more reliable) ──────────────────

def _fetch_grailed(keyword: str, max_usd: float) -> list:
    """
    Grailed uses Algolia for search. Query it directly — no browser needed.
    Public search-only key embedded in their JS bundle.
    """
    url = "https://mnrwefss2q-dsn.algolia.net/1/indexes/*/queries"
    headers = {
        "X-Algolia-Application-Id": "MNRWEFSS2Q",
        "X-Algolia-API-Key": "c89dbaddf15fe70e1941a109bf7c2a3d",
        "Content-Type": "application/json",
        "Referer": "https://www.grailed.com/",
        "Origin": "https://www.grailed.com",
    }
    body = {
        "requests": [{
            "indexName": "Listing_production",
            "params": urllib.parse.urlencode({
                "query": keyword,
                "hitsPerPage": 24,
                "attributesToRetrieve": "id,title,designer,price,size,cover_photo",
                "numericFilters": f"price_i<={int(max_usd * 100)}",
            })
        }]
    }
    try:
        resp = _requests.post(url, json=body, headers=headers, timeout=15)
        resp.raise_for_status()
        hits = (resp.json().get("results") or [{}])[0].get("hits", [])
    except Exception as e:
        print(f"Grailed Algolia error: {e}")
        hits = []

    out = []
    for h in hits:
        try:
            price_cents = h.get("price_i") or (h.get("price", 0) * 100)
            price_usd = price_cents / 100
            if price_usd <= 0 or price_usd > max_usd:
                continue
            aud = round(price_usd * USD_TO_AUD)
            designer = (h.get("designer") or {})
            brand = designer.get("name") or keyword.title()
            listing_id = h.get("id") or h.get("objectID", "")
            photo = ""
            cover = h.get("cover_photo") or {}
            if cover:
                photo = cover.get("url") or cover.get("thumb") or ""
            out.append({
                "brand": brand,
                "item": (h.get("title") or "").strip() or keyword.title(),
                "desc": "",
                "size": h.get("size") or "Check listing",
                "origPrice": f"USD {price_usd:.0f}",
                "aud": aud,
                "under": aud < 250,
                "site": "Grailed",
                "url": f"https://www.grailed.com/listings/{listing_id}",
                "rep": "auth",
                "notes": "Live Grailed listing",
                "image": photo,
            })
        except Exception:
            continue
    return out[:12]


async def scrape_grailed(keyword: str, max_usd: float) -> list:
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, _fetch_grailed, keyword, max_usd)
    if results:
        return results
    # Playwright fallback if Algolia fails
    return await _grailed_playwright(keyword, max_usd)


async def _grailed_playwright(keyword: str, max_usd: float) -> list:
    slug = urllib.parse.quote_plus(keyword)
    url = f"https://www.grailed.com/shop?query={slug}"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=BROWSER_ARGS)
            ctx = await browser.new_context(user_agent=UA, locale="en-US")
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                await page.wait_for_selector('a[href*="/listings/"]', timeout=15000)
            except Exception:
                pass
            await page.wait_for_timeout(3000)

            cards = await page.evaluate("""
                () => {
                    const anchors = Array.from(document.querySelectorAll('a[href*="/listings/"]'));
                    const results = [];
                    const seen = new Set();
                    for (const a of anchors) {
                        const href = a.href || '';
                        const m = href.match(/\\/listings\\/(\\d+)/);
                        if (!m || seen.has(m[1])) continue;
                        seen.add(m[1]);
                        let node = a, price = '', img = '', title = '';
                        for (let i = 0; i < 8; i++) {
                            if (!node) break;
                            const spans = Array.from(node.querySelectorAll('span'))
                                .filter(s => s.textContent.includes('$') && s.textContent.trim().length < 20);
                            if (spans.length) {
                                price = spans[0].textContent.trim();
                                const imgEl = node.querySelector('img');
                                img = imgEl ? (imgEl.src || imgEl.getAttribute('src') || '') : '';
                                const titleEl = node.querySelector('p,h3,h4,[class*="title"],[class*="Title"]');
                                title = titleEl ? titleEl.textContent.trim() : '';
                                break;
                            }
                            node = node.parentElement;
                        }
                        results.push({ href, id: m[1], price, img, title });
                    }
                    return results;
                }
            """)
            await browser.close()
    except Exception as e:
        print(f"Grailed Playwright fallback error: {e}")
        return []

    out = []
    for c in cards:
        try:
            pm = re.search(r'[\d,]+(?:\.\d+)?', c["price"].replace(",", ""))
            if not pm:
                continue
            price_usd = float(pm.group())
            if price_usd > max_usd:
                continue
            aud = round(price_usd * USD_TO_AUD)
            dom_title = (c.get("title") or "").strip()
            slug_match = re.search(r'/listings/\d+-(.+?)(?:\?|$)', c["href"])
            slug_title = slug_match.group(1).replace("-", " ").title() if slug_match else ""
            title = dom_title or slug_title or keyword.title()
            raw_img = c["img"].rstrip("?")
            img_url = (raw_img + "?w=640") if raw_img and "?" not in raw_img else raw_img
            out.append({
                "brand": keyword.title(),
                "item": title,
                "desc": "",
                "size": "Check listing",
                "origPrice": f"USD {price_usd:.0f}",
                "aud": aud,
                "under": aud < 250,
                "site": "Grailed",
                "url": f"https://www.grailed.com/listings/{c['id']}",
                "rep": "auth",
                "notes": "Live Grailed listing",
                "image": img_url,
            })
        except Exception:
            continue
    return out[:12]


# ── Depop — JSON API (no Playwright) ──────────────────────────────────────────

def _fetch_depop(keyword: str, max_usd: float) -> list:
    """Depop has a public search API that returns JSON."""
    q = urllib.parse.quote_plus(keyword)
    url = f"https://webapi.depop.com/api/v2/search/products/?q={q}&country=au&currency=AUD&numberOfResults=24&itemsPerPage=24"
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Accept-Language": "en-AU,en;q=0.9",
        "Referer": "https://www.depop.com/",
        "Origin": "https://www.depop.com",
    }
    try:
        resp = _requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        products = resp.json().get("products", [])
    except Exception as e:
        print(f"Depop API error: {e}")
        products = []

    out = []
    for p in products:
        try:
            # Depop API prices are in minor units (cents for AUD)
            price_info = p.get("price") or {}
            price_str = price_info.get("amountRevised") or price_info.get("amount") or "0"
            price_aud = float(str(price_str).replace(",", "")) / 100
            if not price_aud:
                # try national price
                nat = p.get("nationalShippingCost") or {}
                price_aud = float(str(price_info.get("amount", 0)).replace(",", "")) / 100
            if price_aud <= 0:
                continue
            price_usd = price_aud / USD_TO_AUD
            if price_usd > max_usd:
                continue
            slug = p.get("slug") or ""
            pid = p.get("id") or ""
            preview = (p.get("previews") or [{}])[0]
            img = preview.get("src") or preview.get("url") or ""
            title = p.get("description") or slug.replace("-", " ").title() or keyword.title()
            if len(title) > 80:
                title = title[:77] + "…"
            out.append({
                "brand": "",
                "item": title,
                "desc": "",
                "size": (p.get("sizes") or [{}])[0].get("label") or "Check listing",
                "origPrice": f"AUD {price_aud:.0f}",
                "aud": round(price_aud),
                "under": price_aud < 250,
                "site": "Depop",
                "url": f"https://www.depop.com/products/{slug or pid}/",
                "rep": "auth",
                "notes": "Live Depop listing",
                "image": img,
            })
        except Exception:
            continue
    return out[:12]


async def scrape_depop(keyword: str, max_usd: float) -> list:
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, _fetch_depop, keyword, max_usd)
    if results:
        return results
    # Playwright fallback
    return await _depop_playwright(keyword, max_usd)


async def _depop_playwright(keyword: str, max_usd: float) -> list:
    q = urllib.parse.quote_plus(keyword)
    url = f"https://www.depop.com/search/?q={q}"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=BROWSER_ARGS)
            ctx = await browser.new_context(user_agent=UA, locale="en-US")
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                await page.wait_for_selector('a[href*="/products/"]', timeout=15000)
            except Exception:
                pass
            await page.wait_for_timeout(3000)

            cards = await page.evaluate("""
                () => {
                    const results = [], seen = new Set();
                    const anchors = Array.from(document.querySelectorAll('a[href*="/products/"]'));
                    for (const a of anchors) {
                        const href = a.getAttribute('href') || '';
                        if (!href || seen.has(href)) continue;
                        seen.add(href);
                        let price = '', img = '', node = a;
                        for (let i = 0; i < 10; i++) {
                            if (!node) break;
                            const priceEls = Array.from(node.querySelectorAll('p,span,div'))
                                .filter(el => el.children.length === 0)
                                .filter(el => /[\\$\\£\\€]|\\d+\\.\\d{2}/.test(el.textContent) && el.textContent.trim().length < 20);
                            if (priceEls.length) {
                                price = priceEls[0].textContent.trim();
                                const imgEl = node.querySelector('img[src]');
                                img = imgEl ? imgEl.src : '';
                                break;
                            }
                            node = node.parentElement;
                        }
                        results.push({ href, price, img });
                    }
                    return results;
                }
            """)
            await browser.close()
    except Exception as e:
        print(f"Depop Playwright error: {e}")
        return []

    out = []
    for c in cards:
        try:
            pm = re.search(r'[\d,]+(?:\.\d+)?', c["price"].replace(",", ""))
            if not pm:
                continue
            price_usd = float(pm.group())
            if not price_usd or price_usd > max_usd:
                continue
            aud = round(price_usd * USD_TO_AUD)
            href = c["href"]
            slug = href.strip("/").split("/")[-1]
            parts = slug.split("-")
            inner = parts[1:] if len(parts) > 1 else parts
            if inner and re.fullmatch(r'[0-9a-fA-F]{4,}', inner[-1]):
                inner = inner[:-1]
            title = " ".join(inner).title() if inner else keyword.title()
            url = f"https://www.depop.com{href}" if href.startswith("/") else href
            out.append({
                "brand": "",
                "item": title,
                "desc": "",
                "size": "Check listing",
                "origPrice": f"USD {price_usd:.0f}",
                "aud": aud,
                "under": aud < 250,
                "site": "Depop",
                "url": url,
                "rep": "auth",
                "notes": "Live Depop listing",
                "image": c["img"],
            })
        except Exception:
            continue
    return out[:12]


# ── eBay AU — requests + BeautifulSoup ────────────────────────────────────────

def _parse_ebay_soup(soup, max_aud: float, keyword: str) -> list:
    kw_words = _kw_words(keyword)

    # Try selector sets in order until one yields results
    SELECTOR_SETS = [
        ("ul.srp-results li.s-item:not(.s-item--large)", ".s-item__title", ".s-item__price", "a.s-item__link"),
        ("ul.srp-results li.s-card", ".s-card__title", ".s-card__price", "a.s-card__link"),
        (".srp-results li[class*='s-item']", "[class*='title']", "[class*='price']", "a[href*='ebay.com']"),
    ]

    for card_sel, title_sel, price_sel, link_sel in SELECTOR_SETS:
        cards = soup.select(card_sel)
        if not cards:
            continue
        print(f"eBay: {len(cards)} cards with '{card_sel}'")
        out = []
        for card in cards:
            try:
                title_el = card.select_one(title_sel)
                if not title_el:
                    continue
                title = re.sub(r"Opens?\s+in\s+a\s+new.*", "", title_el.get_text(strip=True), flags=re.IGNORECASE).strip()
                if not title or title.lower() in ("shop on ebay", ""):
                    continue
                if not _relevant(title, kw_words):
                    continue

                price_el = card.select_one(price_sel)
                if not price_el:
                    continue
                price_text = re.split(r"\s+to\s+|–", price_el.get_text(strip=True))[0]
                pm = re.search(r"[\d,]+(?:\.\d+)?", price_text.replace(",", ""))
                if not pm:
                    continue
                price_aud = float(pm.group())
                if price_aud < 1 or price_aud > max_aud:
                    continue

                link_el = card.select_one(link_sel) or card.select_one("a[href]")
                href = link_el["href"] if link_el and link_el.get("href") else ""
                img_el = card.select_one("img")
                img = (img_el.get("src") or img_el.get("data-src", "")) if img_el else ""

                out.append({
                    "brand": "",
                    "item": title,
                    "desc": "",
                    "size": "Check listing",
                    "origPrice": f"AUD {price_aud:.0f}",
                    "aud": round(price_aud),
                    "under": price_aud < 250,
                    "site": "eBay AU",
                    "url": href,
                    "rep": "auth",
                    "notes": "Live eBay AU listing",
                    "image": img,
                })
            except Exception:
                continue
        if out:
            return out[:12]

    return []


def _fetch_ebay(keyword: str, max_aud: float) -> list:
    q = urllib.parse.quote_plus(keyword)
    hdrs = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-AU,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Upgrade-Insecure-Requests": "1",
    }
    sess = _requests.Session()
    sess.headers.update(hdrs)
    try:
        sess.get("https://www.ebay.com.au/", timeout=15)
    except Exception:
        pass

    for sacat in ["1059", "0"]:
        url = f"https://www.ebay.com.au/sch/i.html?_nkw={q}&_sacat={sacat}&_sop=15&_ipg=60"
        try:
            resp = sess.get(url, timeout=30)
            soup = BeautifulSoup(resp.text, "lxml")
            result = _parse_ebay_soup(soup, max_aud, keyword)
            if result:
                return result
        except Exception as e:
            print(f"eBay sacat={sacat} error: {e}")

    return []


async def scrape_ebay(keyword: str, max_usd: float) -> list:
    max_aud = round(max_usd * USD_TO_AUD)
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _fetch_ebay, keyword, max_aud)
    except Exception as e:
        print(f"eBay error: {e}")
        return []


# ── Buyee ──────────────────────────────────────────────────────────────────────

async def scrape_buyee(keyword: str, max_usd: float) -> list:
    max_jpy = round(max_usd * USD_TO_AUD * JPY_PER_AUD)
    q = urllib.parse.quote_plus(keyword)
    url = f"https://buyee.jp/item/search/query/{q}?translationType=1"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=BROWSER_ARGS)
            ctx = await browser.new_context(user_agent=UA, locale="en-US")
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            # wait for item cards to appear instead of networkidle (Buyee never goes idle)
            try:
                await page.wait_for_selector('a[href*="/item/"]', timeout=20000)
            except Exception:
                pass
            await page.wait_for_timeout(4000)

            cards = await page.evaluate("""
                () => {
                    const results = [], seen = new Set();
                    let anchors = Array.from(document.querySelectorAll(
                        'a[href*="/item/jdirectitems/"], a[href*="/mercari/item/m"]'
                    ));
                    if (anchors.length === 0) {
                        anchors = Array.from(document.querySelectorAll('a[href*="/item/"]'))
                            .filter(a => a.href && !a.href.includes('/search/') && !a.href.includes('/category/'));
                    }
                    for (const a of anchors) {
                        const href = a.getAttribute('href') || a.href || '';
                        if (!href || seen.has(href)) continue;
                        seen.add(href);
                        let node = a, price = '', img = '', title = '';
                        for (let i = 0; i < 12; i++) {
                            if (!node) break;
                            const txt = node.innerText || '';
                            const m = txt.match(/[¥￥][\\d,]+|[\\d,]{3,}円/);
                            if (m) {
                                price = m[0];
                                const imgEl = node.querySelector('img[src]');
                                img = imgEl ? imgEl.src : '';
                                const lines = txt.split('\\n')
                                    .map(l => l.trim())
                                    .filter(l => l.length > 5 && l.length < 120
                                        && !/[¥￥]/.test(l) && !/^[\\d,]+$/.test(l));
                                title = lines[0] || '';
                                break;
                            }
                            node = node.parentElement;
                        }
                        if (price) results.push({ href, price, img, title });
                    }
                    return results;
                }
            """)
            await browser.close()
    except Exception as e:
        print(f"Buyee error: {e}")
        return []

    print(f"Buyee raw cards: {len(cards)}")
    out = []
    for c in cards:
        try:
            pm = re.search(r'[\d,]+', c["price"].replace(",", ""))
            if not pm:
                continue
            price_jpy = float(pm.group())
            if not price_jpy or price_jpy > max_jpy:
                continue
            aud = round(price_jpy / JPY_PER_AUD)
            href = c["href"]
            item_url = f"https://buyee.jp{href}" if href.startswith("/") else href
            source = "Buyee (Mercari JP)" if "/mercari/" in href else "Buyee (Yahoo JP)"
            out.append({
                "brand": "",
                "item": c["title"] or keyword.title(),
                "desc": "",
                "size": "Check listing",
                "origPrice": f"¥{price_jpy:,.0f}",
                "aud": aud,
                "under": aud < 250,
                "site": source,
                "url": item_url,
                "rep": "auth",
                "notes": f"¥{price_jpy:,.0f} JPY (~AUD {aud}). Buy via Buyee proxy (+5-10% commission). JP sizing — ask seller for measurements.",
                "image": c["img"],
            })
        except Exception:
            continue
    return out[:12]


# ── Yahoo Auctions Japan ───────────────────────────────────────────────────────

async def _yahoo_fetch_url(url: str) -> list:
    """Run one Yahoo JP Playwright fetch, return raw card dicts."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=BROWSER_ARGS)
        ctx = await browser.new_context(user_agent=UA, locale="en-US")
        page = await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_selector('a[href*="/jp/auction/"]', timeout=15000)
        except Exception:
            pass
        await page.wait_for_timeout(4000)
        cards = await page.evaluate("""
            () => {
                const results = [], seen = new Set();
                const anchors = Array.from(document.querySelectorAll('a[href*="/jp/auction/"]'));
                for (const a of anchors) {
                    const href = a.href || a.getAttribute('href') || '';
                    if (!href || seen.has(href)) continue;
                    seen.add(href);
                    let node = a, price = '', img = '', title = '';
                    for (let i = 0; i < 12; i++) {
                        if (!node) break;
                        const txt = node.innerText || '';
                        const m = txt.match(/[¥￥][\\d,]+|[\\d,]{3,}円/);
                        if (m) {
                            price = m[0];
                            const imgEl = node.querySelector('img[src]');
                            img = imgEl ? imgEl.src : '';
                            const lines = txt.split('\\n')
                                .map(l => l.trim())
                                .filter(l => l.length > 3 && l.length < 120
                                    && !/[¥￥]/.test(l) && !/^[\\d,]+$/.test(l));
                            title = lines[0] || '';
                            break;
                        }
                        node = node.parentElement;
                    }
                    if (price) results.push({ href, price, img, title });
                }
                return results;
            }
        """)
        await browser.close()
    return cards


async def scrape_yahoo_jp(keyword: str, max_usd: float) -> list:
    max_jpy = round(max_usd * USD_TO_AUD * JPY_PER_AUD)
    q = urllib.parse.quote_plus(keyword)

    # try Men's Fashion category first, fall back to all categories
    urls_to_try = [
        f"https://auctions.yahoo.co.jp/search/search?p={q}&ei=UTF-8&auccat=2084005&sorder=1",
        f"https://auctions.yahoo.co.jp/search/search?p={q}&ei=UTF-8&sorder=1",
    ]

    cards = []
    for url in urls_to_try:
        try:
            cards = await _yahoo_fetch_url(url)
            print(f"Yahoo JP raw cards: {len(cards)} from {url}")
            if cards:
                break
        except Exception as e:
            print(f"Yahoo JP error ({url}): {e}")

    out = []
    for c in cards:
        try:
            pm = re.search(r'[\d,]+', c["price"].replace(",", ""))
            if not pm:
                continue
            price_jpy = float(pm.group())
            if not price_jpy or price_jpy > max_jpy:
                continue
            aud = round(price_jpy / JPY_PER_AUD)
            href = c["href"]
            auction_id_m = re.search(r'/auction/([a-zA-Z0-9]+)', href)
            item_url = (
                f"https://buyee.jp/item/yahoo/auction/{auction_id_m.group(1)}"
                if auction_id_m else href
            )
            out.append({
                "brand": "",
                "item": c["title"] or keyword.title(),
                "desc": "",
                "size": "Check listing",
                "origPrice": f"¥{price_jpy:,.0f}",
                "aud": aud,
                "under": aud < 250,
                "site": "Yahoo Auctions JP",
                "url": item_url,
                "rep": "auth",
                "notes": f"¥{price_jpy:,.0f} JPY (~AUD {aud}). Link goes to Buyee proxy. JP sizing — ask seller for measurements.",
                "image": c["img"],
            })
        except Exception:
            continue
    return out[:12]


# ── Routes ─────────────────────────────────────────────────────────────────────

SCRAPERS = {
    "grailed":   scrape_grailed,
    "depop":     scrape_depop,
    "ebay":      scrape_ebay,
    "buyee":     scrape_buyee,
    "yahoo_jp":  scrape_yahoo_jp,
}


@app.get("/scrape")
async def scrape(keyword: str = "", site: str = "grailed", max_price: int = 500):
    keyword = keyword.strip()
    if not keyword:
        return {"items": [], "count": 0, "error": "keyword required"}
    max_usd = round(max_price / USD_TO_AUD)
    fn = SCRAPERS.get(site)
    if not fn:
        return {"items": [], "count": 0, "error": f"unknown site: {site}"}
    try:
        results = await fn(keyword, max_usd)
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
