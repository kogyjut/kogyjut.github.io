# Scraper Reference

Covers `scraper.py` — the scheduled background scraper that runs via GitHub Actions.  
This is separate from `api.py` (the live user-triggered API).

---

## What It Does

Runs on a schedule, searches Grailed for a fixed list of archive brands, filters by price, and writes the results to `data.json` in the repo. The site reads `data.json` on page load to populate the main grid.

---

## How to Run It

**Via GitHub Actions (normal, automatic):**  
It runs every 8 hours. You can also trigger it manually:  
Repo → Actions → Scrape Clothing Data → Run workflow

**Locally:**
```powershell
cd C:\Users\nathan\Desktop\kogyjut-update
python scraper.py
```
This writes `data.json` to the same folder. The local site at `localhost:8080` will pick it up on refresh.

---

## Brands It Searches

Defined in `scraper.py` in the `SEARCHES` list:

```python
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
```

Each entry is `(search_term, rep_type)` where `rep_type` is `"auth"` or `"rep"`.

**To add a brand:**
```python
SEARCHES = [
    ...
    ("your brand here", "auth"),
]
```

**To remove a brand:** delete its line from the list.

---

## Price Limit

```python
MAX_AUD = 500
USD_TO_AUD = 1.55
```

`MAX_AUD` is the ceiling in AUD. It's converted to USD before filtering:
```python
max_usd = round(MAX_AUD / USD_TO_AUD)  # → ~322 USD
```

To change the limit, edit `MAX_AUD` at the top of `scraper.py`.

Note: `USD_TO_AUD` is a hardcoded approximation. To use a live rate you'd need a currency API — not currently implemented.

---

## How Grailed is Scraped

The scraper uses Scrapling's `StealthyFetcher` which runs a headless stealth browser (Camoufox):

```python
page = StealthyFetcher.fetch(
    url,
    headless=True,
    network_idle=True,
    timeout=45000,
)
```

It then tries two methods to extract listing data:

**Method 1 — `__NEXT_DATA__` (preferred)**  
Grailed's site is built with Next.js. Next.js embeds server-rendered data in a `<script id="__NEXT_DATA__">` tag. The scraper looks for this and parses the JSON directly.

```python
next_data = page.css("script#__NEXT_DATA__")
if next_data:
    data = json.loads(next_data[0].text)
    listings = data.get("props", {}).get("pageProps", {}).get("listings", [])
```

**Method 2 — CSS selectors (fallback)**  
If `__NEXT_DATA__` isn't present or has no listings, the scraper tries known CSS selector patterns:
```python
for sel in [
    "article.listing-card",
    "[data-testid='listing-card']",
    "[class*='FeedItem']",
    ".feed-item",
]:
    cards = page.css(sel)
    if cards:
        break
```

---

## Items Per Brand

```python
return results[:4]  # max 4 items per brand search
```

With 9 brands × up to 4 items = up to 36 items in `data.json`.

---

## What Happens If Scraping Fails

If a brand search returns 0 results (site blocked, network error, selector mismatch):
- That brand is skipped silently
- Other brands continue
- If **all** brands return 0 results, `data.json` is NOT overwritten — the old data is preserved

```python
if not all_items:
    print("\nScraper got 0 results — keeping existing data.json")
    return
```

This prevents the site from going blank if Grailed temporarily blocks the scraper.

---

## GitHub Actions Schedule

Defined in `.github/workflows/scrape.yml`:

```yaml
on:
  schedule:
    - cron: "0 */8 * * *"   # every 8 hours
  workflow_dispatch:          # manual trigger
```

**To change the frequency:**  
Edit the cron expression. Examples:
```
"0 */4 * * *"    # every 4 hours
"0 0 * * *"      # once daily at midnight UTC
"0 9,21 * * *"   # 9am and 9pm UTC
```

**GitHub Actions free tier:**  
2,000 minutes/month. Each scraper run takes ~3 minutes. At every-8-hours that's 3 × 90 = 270 runs/month = ~810 minutes/month. Well within the free limit.

---

## GitHub Actions Workflow Steps

```yaml
steps:
  - Checkout repo
  - Setup Python 3.11
  - Cache pip (speeds up repeat runs)
  - pip install scrapling[camoufox]
  - scrapling install camoufox --headless   # downloads browser binary
  - python scraper.py
  - git add data.json
  - git commit (only if data.json changed)
  - git push
```

The `git diff --staged --quiet` check means a commit is only made when results actually changed. This avoids polluting the commit history on runs where nothing new was found.

---

## The data.json Output

Example of what a successful scraper run produces:

```json
{
  "items": [
    {
      "n": 1,
      "brand": "Rick Owens",
      "item": "Rick Owens DRKSHDW Detroit Jeans",
      "desc": "Heavy denim, dropped crotch, raw hem",
      "size": "M",
      "origPrice": "USD 180",
      "aud": 279,
      "under": false,
      "site": "Grailed",
      "url": "https://www.grailed.com/listings/12345678",
      "rep": "auth",
      "notes": "Live Grailed listing — click to view current price",
      "image": "https://cdn.grailed.com/..."
    }
  ],
  "last_updated": "May 2026",
  "count": 28,
  "scraped_at": "2026-05-13T08:00:12.431200"
}
```

---

## Keeping the Scraper Working Long-Term

Scrapers break when websites change their HTML structure. Signs the scraper has stopped working:

- GitHub Action runs successfully but `data.json` isn't updating (0 items found)
- The site shows old data that never changes

**How to diagnose:**

1. Go to repo → Actions → click the latest "Scrape Clothing Data" run
2. Expand the "Run scraper" step
3. Look for `Got 0 items` for all brands — this means selectors aren't matching

**How to fix:**

1. Manually visit a Grailed search page in your browser
2. Open DevTools (F12) → Inspector
3. Find the listing card element and get its CSS selector or class name
4. Update the selector list in `scraper.py`:

```python
for sel in [
    "article.listing-card",         # ← update these
    "[data-testid='listing-card']",
    "[class*='FeedItem']",
    ".feed-item",
    ".your-new-selector-here",      # ← add new one at top
]:
```
