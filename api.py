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

# ── Keyword relevance helpers ──────────────────────────────────────────────────

_STOP = {"a","an","the","and","or","for","in","on","at","to","of","is","it","its","with","by","from","s"}

def _kw_words(keyword: str) -> list:
    return [w for w in keyword.lower().split() if w not in _STOP and len(w) >= 3]

def _relevant(title: str, kw_words: list) -> bool:
    if not kw_words:
        return True
    t = title.lower()
    return any(w in t for w in kw_words)

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
                        let price = '', img = '', title = '';
                        for (let i = 0; i < 8; i++) {
                            if (!node) break;
                            const spans = Array.from(node.querySelectorAll('span'))
                                .filter(s => s.textContent.includes('$') && s.textContent.trim().length < 20);
                            if (spans.length) {
                                price = spans[0].textContent.trim();
                                const imgEl = node.querySelector('img');
                                img = imgEl ? (imgEl.src || imgEl.getAttribute('src') || '') : '';
                                // get real title text from p/h tags near the card
                                const titleEl = node.querySelector('p,h3,h4,[class*="title"],[class*="Title"]');
                                title = titleEl ? titleEl.textContent.trim() : (a.textContent.trim().split('\\n')[0] || '');
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
        print(f"Grailed error: {e}")
        return []

    kw_words = _kw_words(keyword)
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
            # prefer DOM title, fall back to URL slug
            dom_title = (c.get("title") or "").strip()
            slug_match = re.search(r'/listings/\d+-(.+?)(?:\?|$)', c["href"])
            slug_title = slug_match.group(1).replace("-", " ").title() if slug_match else ""
            title = dom_title or slug_title or keyword.title()
            if not _relevant(title, kw_words):
                continue
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

    kw_words = _kw_words(keyword)
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
            if not _relevant(title, kw_words):
                continue
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

def _parse_ebay_cards(cards, soup, max_aud: float, keyword: str) -> list:
    """Try multiple eBay markup patterns and return parsed items."""
    kw_words = _kw_words(keyword)

    # Selector sets: (card_sel, title_sel, price_sel, link_sel)
    SELECTOR_SETS = [
        # Current eBay AU markup (li.s-item is the standard)
        ("ul.srp-results li.s-item:not(.s-item--large)", ".s-item__title", ".s-item__price", "a.s-item__link"),
        # Alternate card class seen on some eBay pages
        ("ul.srp-results li.s-card", ".s-card__title", ".s-card__price", "a.s-card__link"),
        # Broader fallback
        (".srp-results li[class*='s-item']", "[class*='title']", "[class*='price']", "a[href*='ebay.com']"),
    ]

    out = []
    tried_cards = cards  # start with whatever was pre-selected
    tried_sel_idx = -1

    for sel_idx, (card_sel, title_sel, price_sel, link_sel) in enumerate(SELECTOR_SETS):
        if not tried_cards:
            tried_cards = soup.select(card_sel)
            print(f"eBay selector set {sel_idx}: {len(tried_cards)} cards")
        if not tried_cards:
            continue

        for card in tried_cards:
            try:
                title_el = card.select_one(title_sel)
                if not title_el:
                    continue
                title = re.sub(r"Opens?\s+in\s+a\s+new.*", "", title_el.get_text(strip=True), flags=re.IGNORECASE).strip()
                if not title or title.lower() == "shop on ebay":
                    continue
                if not _relevant(title, kw_words):
                    continue

                price_el = card.select_one(price_sel)
                if not price_el:
                    continue
                price_text = re.split(r"\s+to\s+|–|-", price_el.get_text(strip=True))[0]
                pm = re.search(r"[\d,]+(?:\.\d+)?", price_text.replace(",", ""))
                if not pm:
                    continue
                price_aud = float(pm.group())
                if price_aud < 1 or price_aud > max_aud:
                    continue

                link_el = card.select_one(link_sel) or card.select_one("a[href]")
                href = link_el["href"] if link_el and link_el.get("href") else ""
                img_el = card.select_one("img")
                img = img_el.get("src", "") or img_el.get("data-src", "") if img_el else ""

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
            break
        tried_cards = []  # reset so next iteration tries its own selector

    return out[:12]


def _fetch_ebay(keyword: str, max_aud: float) -> list:
    """
    eBay AU search results are server-rendered HTML — plain requests avoids
    headless-browser bot detection. Tries Men's Clothing category first,
    falls back to all categories if empty, then tries broader selectors.
    """
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
        sess.get("https://www.ebay.com.au/", timeout=15)  # warm up session cookie
    except Exception:
        pass

    # Try Men's Clothing (1059) first, fall back to all categories (0)
    for sacat in ["1059", "0"]:
        url = f"https://www.ebay.com.au/sch/i.html?_nkw={q}&_sacat={sacat}&_sop=15&_ipg=60"
        try:
            resp = sess.get(url, timeout=30)
            soup = BeautifulSoup(resp.text, "lxml")
            # Try the standard s-item selector first for count reporting
            cards = soup.select("ul.srp-results li.s-item:not(.s-item--large)")
            print(f"eBay sacat={sacat}: {len(cards)} s-item cards")
            result = _parse_ebay_cards(cards, soup, max_aud, keyword)
            if result:
                return result
        except Exception as e:
            print(f"eBay sacat={sacat} error: {e}")
            continue

    return []


async def scrape_ebay(keyword: str, max_usd: float) -> list:
    max_aud = round(max_usd * USD_TO_AUD)
    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _fetch_ebay, keyword, max_aud)
    except Exception as e:
        print(f"eBay error: {e}")
        return []


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
    url = f"https://buyee.jp/item/search/query/{q}?translationType=1"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=BROWSER_ARGS)
            ctx = await browser.new_context(user_agent=UA, locale="en-US")
            page = await ctx.new_page()
            # networkidle ensures the React grid has fully rendered
            await page.goto(url, wait_until="networkidle", timeout=60000)
            await page.wait_for_timeout(2000)

            # Buyee Yahoo JP items use /item/jdirectitems/auction/ (confirmed via inspection)
            # Buyee Mercari items use /mercari/item/m…
            # Broader fallback: any /item/ link on buyee.jp
            cards = await page.evaluate("""
                () => {
                    const results = [];
                    const seen = new Set();
                    let anchors = Array.from(document.querySelectorAll(
                        'a[href*="/item/jdirectitems/"], a[href*="/mercari/item/m"]'
                    ));
                    // fallback to any buyee item link if specific selectors return nothing
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
                            const m = txt.match(/[¥￥][\d,]+|[\d,]{3,}円/);
                            if (m) {
                                price = m[0];
                                const imgEl = node.querySelector('img[src]');
                                img = imgEl ? imgEl.src : '';
                                const lines = txt.split('\\n')
                                    .map(l => l.trim())
                                    .filter(l => l.length > 5 && l.length < 120
                                        && !/[¥￥]/.test(l) && !/^[\d,]+$/.test(l));
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
    kw_words = _kw_words(keyword)
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
            title = c["title"] or keyword.title()
            if not _relevant(title, kw_words):
                continue
            href = c["href"]
            item_url = f"https://buyee.jp{href}" if href.startswith("/") else href
            source = "Buyee (Mercari JP)" if "/mercari/" in href else "Buyee (Yahoo JP)"
            out.append({
                "brand": "",
                "item": title,
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
    # Men's Fashion category; sort by end time so active auctions come first
    url = f"https://auctions.yahoo.co.jp/search/search?p={q}&ei=UTF-8&auccat=2084005&sorder=1"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=BROWSER_ARGS)
            ctx = await browser.new_context(user_agent=UA, locale="en-US")
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                # Real link pattern confirmed via local testing: /jp/auction/
                await page.wait_for_selector('a[href*="/jp/auction/"]', timeout=12000)
            except Exception:
                pass
            await page.wait_for_timeout(3000)

            cards = await page.evaluate("""
                () => {
                    const results = [];
                    const seen = new Set();
                    // Yahoo Auctions JP confirmed link pattern: /jp/auction/{id}
                    const anchors = Array.from(document.querySelectorAll('a[href*="/jp/auction/"]'));
                    for (const a of anchors) {
                        const href = a.href || a.getAttribute('href') || '';
                        if (!href || seen.has(href)) continue;
                        seen.add(href);
                        let node = a, price = '', img = '', title = '';
                        for (let i = 0; i < 12; i++) {
                            if (!node) break;
                            const txt = node.innerText || '';
                            const m = txt.match(/[¥￥][\d,]+|[\d,]{3,}円/);
                            if (m) {
                                price = m[0];
                                const imgEl = node.querySelector('img[src]');
                                img = imgEl ? imgEl.src : '';
                                const lines = txt.split('\\n')
                                    .map(l => l.trim())
                                    .filter(l => l.length > 3 && l.length < 120
                                        && !/[¥￥]/.test(l) && !/^[\d,]+$/.test(l));
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
        print(f"Yahoo JP error: {e}")
        return []

    print(f"Yahoo JP raw cards: {len(cards)}")
    kw_words = _kw_words(keyword)
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
            title = c["title"] or keyword.title()
            if not _relevant(title, kw_words):
                continue
            href = c["href"]
            # Build Buyee proxy URL from the Yahoo auction ID so the user can actually bid
            auction_id_m = re.search(r'/auction/([a-zA-Z0-9]+)', href)
            if auction_id_m:
                item_url = f"https://buyee.jp/item/yahoo/auction/{auction_id_m.group(1)}"
            else:
                item_url = href
            out.append({
                "brand": "",
                "item": title,
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
