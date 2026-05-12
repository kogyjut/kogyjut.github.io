# Architecture

## System Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                          USER'S BROWSER                             │
│                                                                     │
│   kogyjut.github.io (GitHub Pages)                                  │
│   ┌──────────────────────────────────────────────────────────────┐  │
│   │  index.html                                                  │  │
│   │                                                              │  │
│   │  On page load:  fetch('data.json')  ──────────────────────► │  │
│   │                          ▲          (scheduled picks grid)   │  │
│   │                          │                                   │  │
│   │  On scrape btn: fetch(API/scrape) ──────────────────────────►│  │
│   └──────────────────────────────────────────────────────────────┘  │
└─────────────┬───────────────────────────────────────┬───────────────┘
              │ reads                                 │ calls
              ▼                                       ▼
┌─────────────────────────┐             ┌─────────────────────────────┐
│   data.json             │             │   api.py  (Render.com)      │
│   (in GitHub repo)      │             │                             │
│                         │             │   GET /scrape               │
│   Written by scraper.py │             │   ?keyword=bape             │
│   every 8 hours via     │             │   &site=grailed             │
│   GitHub Actions        │             │   &max_price=100            │
└─────────────────────────┘             │                             │
              ▲                         │   Uses StealthyFetcher      │
              │ writes                  │   (stealth browser)         │
┌─────────────────────────┐             │   to bypass anti-bot        │
│  GitHub Actions         │             └──────────────�┬──────────────┘
│  (scrape.yml)           │                            │ scrapes
│                         │                            ▼
│  Runs scraper.py        │             ┌─────────────────────────────┐
│  every 8 hours          │             │   Target Sites              │
│                         │             │                             │
│  Commits data.json      │             │   • Grailed (Algolia API)   │
│  back to repo           │             │   • Depop (Next.js)         │
└─────────────────────────┘             │   • eBay AU (HTML)          │
              ▲                         └─────────────────────────────┘
              │ runs
┌─────────────────────────┐
│  scraper.py             │
│                         │
│  Searches Grailed for:  │
│  rick owens             │
│  yohji yamamoto         │
│  undercover             │
│  raf simons             │
│  helmut lang            │
│  number nine            │
│  issey miyake           │
│  comme des garcons      │
│  if six was nine        │
└─────────────────────────┘
```

---

## Two Scraping Paths

There are two completely separate scraping systems that feed the site.

### Path 1 — Scheduled Picks (automatic, runs without user interaction)

```
GitHub Actions (cron: every 8 hours)
  → runs scraper.py
  → scraper searches Grailed for 9 archive brands
  → filters to under ~$320 USD (~$500 AUD)
  → writes data.json to the repo
  → GitHub Pages serves data.json
  → index.html fetches data.json on page load
  → cards appear in the main grid
```

**Result:** The grid always has up-to-date archive picks without anyone doing anything.

### Path 2 — Live User Search (on-demand, triggered by the scrape button)

```
User types keyword + selects site + sets price → clicks Scrape
  → index.html sends GET to Render API
  → api.py receives request
  → StealthyFetcher launches stealth browser
  → browser hits the target site (Grailed / Depop / eBay AU)
  → parses results from HTML or JSON
  → returns items array as JSON
  → index.html renders results in the scrape panel
```

**Result:** User gets real-time results for any keyword on any supported site.

---

## Why Two Separate Systems?

| | Scheduled scraper | Live API |
|---|---|---|
| **When it runs** | Every 8 hours, automated | When a user clicks Scrape |
| **Where it runs** | GitHub Actions (free) | Render.com server |
| **What it scrapes** | Grailed, fixed brand list | Grailed, Depop, eBay AU — any keyword |
| **Output** | `data.json` file in repo | JSON response to the browser |
| **Speed** | Doesn't matter (background) | Matters — user is waiting |
| **Persistent** | Yes, data saved | No, results only shown once |

---

## How the Stealth Browser Works

Scrapling's `StealthyFetcher` wraps Camoufox — a special Firefox build that patches dozens of browser fingerprinting signals so websites can't tell it's automated. Without this, sites like Grailed and eBay return 403 Forbidden or serve empty pages.

```python
page = StealthyFetcher.fetch(url, headless=True, network_idle=True, timeout=45000)
```

- `headless=True` — no visible browser window
- `network_idle=True` — waits for all network requests to finish (important for React/Next.js sites)
- `timeout=45000` — 45 second max wait

---

## data.json Format

This is the contract between `scraper.py` and `index.html`. Both must agree on this shape.

```json
{
  "items": [
    {
      "n": 1,
      "brand": "Rick Owens",
      "item": "Ramones Low White",
      "desc": "Classic Ramones silhouette, white leather upper",
      "size": "EU 42",
      "origPrice": "USD 280",
      "aud": 434,
      "under": false,
      "site": "Grailed",
      "url": "https://www.grailed.com/listings/12345678",
      "rep": "auth",
      "notes": "Live Grailed listing",
      "image": "https://cdn.grailed.com/..."
    }
  ],
  "last_updated": "May 2026",
  "count": 12,
  "scraped_at": "2026-05-13T14:30:00.000000"
}
```

Field reference:

| Field | Type | Values | Notes |
|---|---|---|---|
| `n` | int | 1, 2, 3… | Display number |
| `brand` | string | any | Shown uppercase on card |
| `item` | string | any | Card title |
| `desc` | string | any | Short description |
| `size` | string | any | e.g. "EU 42", "M", "W32" |
| `origPrice` | string | "USD 280" | Original currency string |
| `aud` | int | any | Price in AUD (integer) |
| `under` | bool | true/false | true if aud < 250 |
| `site` | string | any | Platform name |
| `url` | string | URL | Direct listing link |
| `rep` | string | "auth" or "rep" | Authentic or replica |
| `notes` | string | any | Italic text at bottom of card |
| `image` | string | URL or "" | Product photo |

---

## Hosting

| Component | Host | Cost | URL |
|---|---|---|---|
| Website (`index.html`) | GitHub Pages | Free | kogyjut.github.io |
| Scheduled scraper | GitHub Actions | Free (2000 min/month) | Runs in the repo |
| Live API (`api.py`) | Render.com | Free tier | your-app.onrender.com |

### Render Free Tier Limits
- Spins down after 15 minutes of inactivity
- First request after idle takes ~30 seconds to cold-start the server
- 750 hours/month (enough for one always-on service)
- No credit card required
