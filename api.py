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
    max_aud = round(max_usd * USD_TO_AUD)
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
            await page.wait_for_timeout(2000)

            cards = await page.evaluate("""
                () => {
                    const anchors = Array.from(document.querySelectorAll('a[href*="/products/"]'));
                    const results = [];
                    const seen = new Set();
                    for (const a of anchors) {
                        const href = a.getAttribute('href') || '';
                        if (!href || seen.has(href)) continue;
                        seen.add(href);
                        let node = a;
                        let price = '', img = '';
                        for (let i = 0; i < 8; i++) {
                            if (!node) break;
                            const els = Array.from(node.querySelectorAll('p,span'))
                                .filter(s => s.textContent.includes('$') && s.textContent.trim().length < 15);
                            if (els.length) {
                                price = els[0].textContent.trim();
                                const imgs = Array.from(node.querySelectorAll('img'));
                                const main = imgs.find(i => (i.className || '').includes('mainImage')) || imgs[0];
                                img = main ? (main.src || main.getAttribute('src') || '') : '';
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
            pm = re.search(r'[\d.]+', c["price"].replace(",", ""))
            if not pm:
                continue
            price_aud = float(pm.group())
            if not price_aud or price_aud > max_aud:
                continue
            href = c["href"]
            slug = href.strip("/").split("/")[-1]
            parts = slug.split("-")
            inner = parts[1:] if len(parts) > 1 else parts
            if inner and re.fullmatch(r'[0-9a-fA-F]{4,}', inner[-1]):
                inner = inner[:-1]
            title = " ".join(inner).title() if inner else keyword.title()
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


# ── Routes ─────────────────────────────────────────────────────────────────────

SCRAPERS = {
    "grailed": scrape_grailed,
    "depop": scrape_depop,
    "vinted": scrape_vinted,
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
