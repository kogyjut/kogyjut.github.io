import json
import re
from datetime import datetime
from scrapling.fetchers import StealthyFetcher, Fetcher

USD_TO_AUD = 1.55

SEARCHES = [
    ("rick owens", "auth"),
    ("yohji yamamoto", "auth"),
    ("undercover", "auth"),
    ("raf simons", "auth"),
    ("helmut lang", "auth"),
    ("number nine", "auth"),
    ("if six was nine", "auth"),
    ("issey miyake", "auth"),
    ("comme des garcons", "auth"),
]

MAX_USD = 320  # ~500 AUD


def clean_price(text):
    m = re.search(r"[\d,]+(?:\.\d+)?", text.replace(",", ""))
    return float(m.group()) if m else 0


def scrape_grailed(query, rep_type, max_usd=MAX_USD):
    slug = query.replace(" ", "+")
    url = f"https://www.grailed.com/shop/listings?query={slug}&sort=default"
    print(f"  Fetching: {url}")

    try:
        page = StealthyFetcher.fetch(
            url,
            headless=True,
            network_idle=True,
            timeout=45000,
        )

        # Grailed embeds server data in __NEXT_DATA__ script tag
        next_data = page.css("script#__NEXT_DATA__")
        if next_data:
            raw = next_data[0].text
            data = json.loads(raw)
            listings = (
                data.get("props", {})
                .get("pageProps", {})
                .get("listings", [])
            )
            if listings:
                print(f"  Found {len(listings)} via __NEXT_DATA__")
                return parse_next_data(listings, query, rep_type, max_usd)

        # Fallback: try CSS selectors (Grailed React class names change, so try several)
        selector_sets = [
            ("article.listing-card", "h3", "[class*='price']", "a"),
            ("[data-testid='listing-card']", "p", "[class*='price']", "a"),
            ("[class*='FeedItem']", "p", "[class*='price']", "a"),
            (".feed-item", "p", "[class*='price']", "a"),
        ]

        for container, title_sel, price_sel, link_sel in selector_sets:
            cards = page.css(container)
            if not cards:
                continue
            print(f"  Found {len(cards)} cards with '{container}'")
            return parse_cards(cards, query, rep_type, max_usd, title_sel, price_sel)

        print(f"  No matching elements found for '{query}'")
        return []

    except Exception as e:
        print(f"  ERROR for '{query}': {e}")
        return []


def parse_next_data(listings, query, rep_type, max_usd):
    results = []
    for l in listings:
        try:
            price = float(l.get("price", 0))
            if price > max_usd:
                continue
            aud = round(price * USD_TO_AUD)
            designer = l.get("designer", {}) or {}
            brand = designer.get("name") or query.title()
            item_id = l.get("id", "")
            url = f"https://www.grailed.com/listings/{item_id}"
            photo = ""
            cover = l.get("cover_photo") or {}
            if cover:
                photo = cover.get("url") or cover.get("thumb") or ""
            results.append({
                "brand": brand,
                "item": (l.get("title") or "").strip(),
                "desc": (l.get("description") or "")[:140].strip(),
                "size": (l.get("size") or "Check listing"),
                "origPrice": f"USD {price:.0f}",
                "aud": aud,
                "under": aud < 250,
                "site": "Grailed",
                "url": url,
                "rep": rep_type,
                "notes": "Live Grailed listing — click to view current price",
                "image": photo,
            })
        except Exception:
            continue
    return results[:4]


def parse_cards(cards, query, rep_type, max_usd, title_sel, price_sel):
    results = []
    for card in cards:
        try:
            title_el = card.css(title_sel)
            title = title_el[0].text.strip() if title_el else ""
            price_el = card.css(price_sel)
            price_text = price_el[0].text.strip() if price_el else "0"
            price = clean_price(price_text)
            if price > max_usd:
                continue
            link_el = card.css("a")
            href = link_el[0].attrib.get("href", "") if link_el else ""
            url = f"https://www.grailed.com{href}" if href.startswith("/") else href
            img_el = card.css("img")
            img = img_el[0].attrib.get("src", "") if img_el else ""
            aud = round(price * USD_TO_AUD)
            results.append({
                "brand": query.title(),
                "item": title,
                "desc": "",
                "size": "Check listing",
                "origPrice": f"USD {price:.0f}",
                "aud": aud,
                "under": aud < 250,
                "site": "Grailed",
                "url": url or f"https://www.grailed.com/shop/listings?query={query.replace(' ', '+')}",
                "rep": rep_type,
                "notes": "Live Grailed listing — click to view current price",
                "image": img,
            })
        except Exception:
            continue
    return results[:4]


def load_fallback():
    try:
        with open("data.json", encoding="utf-8") as f:
            return json.load(f).get("items", [])
    except Exception:
        return []


def main():
    print("=== Scrapling Clothing Scraper ===")
    all_items = []

    for query, rep_type in SEARCHES:
        print(f"\nSearching: {query}")
        results = scrape_grailed(query, rep_type)
        print(f"  Got {len(results)} items")
        all_items.extend(results)

    if not all_items:
        print("\nScraper got 0 results — keeping existing data.json")
        return

    # Assign sequential numbers
    for i, item in enumerate(all_items, 1):
        item["n"] = i

    output = {
        "items": all_items,
        "last_updated": datetime.now().strftime("%B %Y"),
        "count": len(all_items),
        "scraped_at": datetime.now().isoformat(),
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\nSaved {len(all_items)} items to data.json")


if __name__ == "__main__":
    main()
