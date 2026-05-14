import json
import re
import uuid
import asyncio
import urllib.parse
from datetime import datetime
from pathlib import Path

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


def _load_wall():
    if WALL_FILE.exists():
        try:
            return json.loads(WALL_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"posts": []}


def _save_wall(data):
    WALL_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Browser helpers ────────────────────────────────────────────────────────────

BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--no-first-run",
    "--no-zygote",
    "--single-process",
]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


async def _pw_get(url: str, wait_selector: str | None = None) -> tuple:
    """
    Returns (page_content_html, pw_page) — caller must close the browser.
    Actually returns raw extracted data via JS to avoid keeping browser open long.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=BROWSER_ARGS)
        ctx = await browser.new_context(user_agent=UA, locale="en-US")
        page = await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        if wait_selector:
            try:
                await page.wait_for_selector(wait_selector, timeout=15000)
            except Exception:
                pass
        else:
            await page.wait_for_timeout(3000)
        html = await page.content()
        await browser.close()
    return html


# ── Grailed ────────────────────────────────────────────────────────────────────

async def scrape_grailed(keyword: str, max_usd: float) -> list:
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
            await page.wait_for_timeout(2000)

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
                        let node = a;
                        let price = '', img = '';
                        for (let i = 0; i < 8; i++) {
                            if (!node) break;
                            const spans = Array.from(node.querySelectorAll('span'))
                                .filter(s => s.textContent.includes('$') && s.textContent.trim().length < 20);
                            if (spans.length) {
                                price = spans[0].textContent.trim();
                                const imgEl = node.querySelector('img');
                                img = imgEl ? (imgEl.src || imgEl.getAttribute('src') || '') : '';
                                break;
                            }
                            node = node.parentElement;
                        }
                        results.push({ href, id: m[1], price, img });
                    }
                    return results;
                }
            """)
            await browser.close()
    except Exception as e:
        print(f"Grailed error: {e}")
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
            slug_match = re.search(r'/listings/\d+-(.+?)(?:\?|$)', c["href"])
            title = slug_match.group(1).replace("-", " ").title() if slug_match else keyword.title()
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


# ── Depop ──────────────────────────────────────────────────────────────────────

async def scrape_depop(keyword: str, max_usd: float) -> list:
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
                    const results = [];
                    const seen = new Set();

                    // Depop wraps each product in an <a href="/products/..."> or a parent li/article
                    // Walk every product anchor and climb up to find price + image
                    const anchors = Array.from(document.querySelectorAll('a[href*="/products/"]'));
                    for (const a of anchors) {
                        const href = a.getAttribute('href') || '';
                        if (!href || seen.has(href)) continue;
                        seen.add(href);

                        // Climb up to find a container that has both a price and an image
                        let price = '', img = '';
                        let node = a;
                        for (let i = 0; i < 10; i++) {
                            if (!node) break;
                            // Price: any leaf text node containing a currency symbol or digit pattern
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
        print(f"Depop error: {e}")
        return []

    out = []
    for c in cards:
        try:
            pm = re.search(r'[\d,]+(?:\.\d+)?', c["price"].replace(",", ""))
            if not pm:
                continue
            # Render is US-based so Depop returns USD prices; convert to AUD
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


# ── eBay AU ────────────────────────────────────────────────────────────────────

async def scrape_ebay(keyword: str, max_usd: float) -> list:
    max_aud = round(max_usd * USD_TO_AUD)
    q = urllib.parse.quote_plus(keyword)
    # Clothing category (11450), Buy It Now only, sort by lowest price
    url = f"https://www.ebay.com.au/sch/i.html?_nkw={q}&_sacat=11450&LH_BIN=1&_sop=15"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=BROWSER_ARGS)
            ctx = await browser.new_context(user_agent=UA, locale="en-AU")
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                await page.wait_for_selector('li.s-item', timeout=15000)
            except Exception:
                pass
            await page.wait_for_timeout(2000)

            cards = await page.evaluate("""
                () => {
                    const items = Array.from(document.querySelectorAll('li.s-item'));
                    const results = [];
                    for (const item of items) {
                        const titleEl = item.querySelector('.s-item__title');
                        // eBay injects a dummy "Shop on eBay" first card
                        if (!titleEl || titleEl.textContent.trim() === 'Shop on eBay') continue;

                        const priceEl = item.querySelector('.s-item__price');
                        const linkEl = item.querySelector('a.s-item__link');
                        const imgEl = item.querySelector('img.s-item__image-img, .s-item__image img');

                        const title = titleEl ? titleEl.textContent.trim() : '';
                        const price = priceEl ? priceEl.textContent.trim() : '';
                        const href = linkEl ? linkEl.href : '';
                        const img = imgEl ? (imgEl.src || imgEl.getAttribute('src') || '') : '';

                        if (title && price && href) results.push({ title, price, href, img });
                    }
                    return results;
                }
            """)
            await browser.close()
    except Exception as e:
        print(f"eBay error: {e}")
        return []

    out = []
    for c in cards:
        try:
            price_text = c["price"]
            # Handle price ranges (e.g. "AU $10.00 to AU $50.00") — take lower bound
            price_text = re.split(r'\s+to\s+|–', price_text)[0]
            pm = re.search(r'[\d,]+(?:\.\d+)?', price_text.replace(",", ""))
            if not pm:
                continue
            price_aud = float(pm.group())
            if not price_aud or price_aud > max_aud:
                continue
            out.append({
                "brand": "",
                "item": c["title"],
                "desc": "",
                "size": "Check listing",
                "origPrice": f"AUD {price_aud:.0f}",
                "aud": round(price_aud),
                "under": price_aud < 250,
                "site": "eBay AU",
                "url": c["href"],
                "rep": "auth",
                "notes": "Live eBay AU listing",
                "image": c["img"],
            })
        except Exception:
            continue
    return out[:12]


# ── Vinted AU ──────────────────────────────────────────────────────────────────

async def scrape_vinted(keyword: str, max_usd: float) -> list:
    max_aud = round(max_usd * USD_TO_AUD)
    q = urllib.parse.quote_plus(keyword)
    url = f"https://www.vinted.com.au/catalog?search_text={q}"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=BROWSER_ARGS)
            ctx = await browser.new_context(user_agent=UA, locale="en-US")
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                await page.wait_for_selector('a[href*="/items/"]', timeout=15000)
            except Exception:
                pass
            await page.wait_for_timeout(2000)

            cards = await page.evaluate("""
                () => {
                    const anchors = Array.from(document.querySelectorAll('a[href*="/items/"]'));
                    const results = [];
                    const seen = new Set();
                    for (const a of anchors) {
                        const href = a.getAttribute('href') || a.href || '';
                        if (!href || seen.has(href)) continue;
                        seen.add(href);
                        let node = a;
                        let price = '', img = '', title = '';
                        for (let i = 0; i < 8; i++) {
                            if (!node) break;
                            const els = Array.from(node.querySelectorAll('p,span'))
                                .filter(s => s.textContent.includes('$') && s.textContent.trim().length < 15);
                            if (els.length) {
                                price = els[0].textContent.trim();
                                const imgEl = node.querySelector('img');
                                img = imgEl ? (imgEl.src || imgEl.getAttribute('src') || '') : '';
                                const titleEls = Array.from(node.querySelectorAll('p,span,h3'))
                                    .filter(s => !s.textContent.includes('$') && s.textContent.trim().length > 3 && s.textContent.trim().length < 80);
                                title = titleEls.length ? titleEls[0].textContent.trim() : '';
                                break;
                            }
                            node = node.parentElement;
                        }
                        results.push({ href, price, img, title });
                    }
                    return results;
                }
            """)
            await browser.close()
    except Exception as e:
        print(f"Vinted error: {e}")
        return []

    out = []
    for c in cards:
        try:
            pm = re.search(r'[\d.]+', c["price"].replace(",", ""))
            if not pm:
                continue
            price_aud = float(pm.group())
            if not price_aud or price_aud > max_aud:
                continue
            href = c["href"]
            out.append({
                "brand": "",
                "item": c["title"] or keyword.title(),
                "desc": "",
                "size": "Check listing",
                "origPrice": f"AUD {price_aud:.0f}",
                "aud": round(price_aud),
                "under": price_aud < 250,
                "site": "Vinted",
                "url": f"https://www.vinted.com.au{href}" if href.startswith("/") else href,
                "rep": "auth",
                "notes": "Live Vinted listing",
                "image": c["img"],
            })
        except Exception:
            continue
    return out[:12]


# ── Buyee ──────────────────────────────────────────────────────────────────────

async def scrape_buyee(keyword: str, max_usd: float) -> list:
    max_jpy = round(max_usd * USD_TO_AUD * JPY_PER_AUD)
    q = urllib.parse.quote_plus(keyword)
    # translationType=1 auto-translates JP titles to English
    url = f"https://buyee.jp/item/search/query/{q}?translationType=1"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=BROWSER_ARGS)
            ctx = await browser.new_context(user_agent=UA, locale="en-US")
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                await page.wait_for_selector(
                    'a[href*="/item/yahoo/auction/"], a[href*="/mercari/item/"]',
                    timeout=15000,
                )
            except Exception:
                pass
            await page.wait_for_timeout(3000)

            cards = await page.evaluate("""
                () => {
                    const results = [];
                    const seen = new Set();
                    const anchors = Array.from(document.querySelectorAll(
                        'a[href*="/item/yahoo/auction/"], a[href*="/mercari/item/"]'
                    ));
                    for (const a of anchors) {
                        const href = a.getAttribute('href') || a.href || '';
                        if (!href || seen.has(href)) continue;
                        seen.add(href);

                        let price = '', img = '', title = '';
                        let node = a;
                        for (let i = 0; i < 10; i++) {
                            if (!node) break;
                            // Yen price: look for ¥ symbol or large number (3+ digits with commas)
                            const priceEls = Array.from(node.querySelectorAll('*'))
                                .filter(el => el.children.length === 0)
                                .filter(el => {
                                    const t = el.textContent.trim();
                                    return (t.includes('¥') || t.includes('￥') || /^[\d,]{3,}$/.test(t))
                                        && t.length < 20;
                                });
                            if (priceEls.length) {
                                price = priceEls[0].textContent.trim();
                                const imgEl = node.querySelector('img[src]');
                                img = imgEl ? imgEl.src : '';
                                // Title: longest leaf text that isn't a price
                                const titleEls = Array.from(node.querySelectorAll('*'))
                                    .filter(el => el.children.length === 0)
                                    .filter(el => {
                                        const t = el.textContent.trim();
                                        return t.length > 8 && t.length < 140
                                            && !t.includes('¥') && !/^[\d,]+$/.test(t);
                                    })
                                    .sort((a, b) => b.textContent.length - a.textContent.length);
                                title = titleEls.length ? titleEls[0].textContent.trim() : '';
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
            source = "Buyee (Yahoo JP)" if "/yahoo/auction/" in href else "Buyee (Mercari JP)"
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

async def scrape_yahoo_jp(keyword: str, max_usd: float) -> list:
    max_jpy = round(max_usd * USD_TO_AUD * JPY_PER_AUD)
    q = urllib.parse.quote_plus(keyword)
    # Category 2084005 = Men's Fashion; sort by end time (sorder=1) to see active listings
    url = f"https://auctions.yahoo.co.jp/search/search?p={q}&ei=UTF-8&auccat=2084005&sorder=1"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=BROWSER_ARGS)
            ctx = await browser.new_context(user_agent=UA, locale="en-US")
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                await page.wait_for_selector('.Product, li[class*="Product"]', timeout=15000)
            except Exception:
                pass
            await page.wait_for_timeout(3000)

            cards = await page.evaluate("""
                () => {
                    const results = [];
                    const seen = new Set();

                    // Yahoo Auctions JP uses .Product list items
                    const items = Array.from(document.querySelectorAll(
                        'li.Product, .Product__item, [class*="Product"][class*="item"], li[class*="product"]'
                    ));

                    for (const item of items) {
                        const a = item.querySelector('a[href*="page.auctions.yahoo.co.jp"], a[href*="/jp/auction/"]')
                               || item.querySelector('a[href]');
                        if (!a) continue;
                        const href = a.href || a.getAttribute('href') || '';
                        if (!href || seen.has(href)) continue;
                        seen.add(href);

                        const titleEl = item.querySelector('.Product__title, [class*="title"], h3, h2');
                        const priceEl = item.querySelector('.Product__priceValue, [class*="price"], .Price');
                        const imgEl = item.querySelector('img[src]');

                        const title = titleEl ? titleEl.textContent.trim() : '';
                        const price = priceEl ? priceEl.textContent.trim() : '';
                        const img = imgEl ? imgEl.src : '';

                        if (price) results.push({ href, price, img, title });
                    }

                    // Fallback: walk all auction links
                    if (results.length === 0) {
                        const anchors = Array.from(document.querySelectorAll(
                            'a[href*="page.auctions.yahoo.co.jp/jp/auction/"]'
                        ));
                        for (const a of anchors) {
                            const href = a.href || '';
                            if (!href || seen.has(href)) continue;
                            seen.add(href);
                            let price = '', img = '', title = '';
                            let node = a;
                            for (let i = 0; i < 10; i++) {
                                if (!node) break;
                                const priceEls = Array.from(node.querySelectorAll('*'))
                                    .filter(el => el.children.length === 0)
                                    .filter(el => {
                                        const t = el.textContent.trim();
                                        return (t.includes('円') || t.includes('¥') || /^[\d,]{3,}$/.test(t))
                                            && t.length < 20;
                                    });
                                if (priceEls.length) {
                                    price = priceEls[0].textContent.trim();
                                    const imgEl = node.querySelector('img[src]');
                                    img = imgEl ? imgEl.src : '';
                                    const titleEls = Array.from(node.querySelectorAll('*'))
                                        .filter(el => el.children.length === 0)
                                        .filter(el => {
                                            const t = el.textContent.trim();
                                            return t.length > 5 && t.length < 140
                                                && !t.includes('円') && !t.includes('¥') && !/^[\d,]+$/.test(t);
                                        });
                                    title = titleEls.length ? titleEls[0].textContent.trim() : '';
                                    break;
                                }
                                node = node.parentElement;
                            }
                            if (price) results.push({ href, price, img, title });
                        }
                    }
                    return results;
                }
            """)
            await browser.close()
    except Exception as e:
        print(f"Yahoo JP error: {e}")
        return []

    out = []
    for c in cards:
        try:
            # Price can be "12,345円", "¥12,345", or just "12345"
            pm = re.search(r'[\d,]+', c["price"].replace(",", ""))
            if not pm:
                continue
            price_jpy = float(pm.group())
            if not price_jpy or price_jpy > max_jpy:
                continue
            aud = round(price_jpy / JPY_PER_AUD)
            href = c["href"]
            # Build Buyee proxy URL from the Yahoo auction ID so the user can actually bid
            auction_id_m = re.search(r'/auction/([a-zA-Z0-9]+)', href)
            if auction_id_m:
                item_url = f"https://buyee.jp/item/yahoo/auction/{auction_id_m.group(1)}"
            else:
                item_url = href
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
    "grailed": scrape_grailed,
    "depop": scrape_depop,
    "vinted": scrape_vinted,
    "ebay": scrape_ebay,
    "buyee": scrape_buyee,
    "yahoo_jp": scrape_yahoo_jp,
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
