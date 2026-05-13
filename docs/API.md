# API Reference

The live scrape API is a FastAPI server defined in `api.py`.  
When deployed to Render it runs at your Render URL.  
Locally it runs at `http://localhost:8001`.

---

## Endpoints

### `GET /health`

Health check. Returns immediately. Use this to confirm the server is up.

**Request**
```
GET /health
```

**Response**
```json
{ "status": "ok" }
```

---

### `GET /scrape`

Scrapes a clothing marketplace and returns matching listings.

**Request**

```
GET /scrape?keyword={keyword}&site={site}&max_price={max_price}
```

**Query Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `keyword` | string | Yes | — | Search term, e.g. `bape hoodie` |
| `site` | string | No | `grailed` | Which site to scrape. See supported sites below. |
| `max_price` | int | No | `500` | Maximum price in **AUD** |

**Supported Sites**

| `site` value | Platform |
|---|---|
| `grailed` | Grailed — archive/designer menswear |
| `depop` | Depop — streetwear and vintage |
| `ebay` | eBay Australia |

**Response — success**

```json
{
  "items": [
    {
      "n": 1,
      "brand": "BAPE",
      "item": "A Bathing Ape Camo Hoodie",
      "desc": "OG shark hoodie, green camo colorway, good condition",
      "size": "M",
      "origPrice": "AUD 85",
      "aud": 85,
      "under": true,
      "site": "Depop",
      "url": "https://www.depop.com/products/abc123/",
      "rep": "auth",
      "notes": "Live Depop listing",
      "image": "https://d3csawd3nj7e2r.cloudfront.net/..."
    }
  ],
  "count": 1
}
```

**Response — no results**

```json
{
  "items": [],
  "count": 0
}
```

**Response — error**

```json
{
  "items": [],
  "count": 0,
  "error": "keyword required"
}
```

---

## How Each Site is Scraped

### Grailed

1. Fetches `grailed.com` homepage to extract the Algolia search API key from the page HTML
2. Calls the Algolia search API directly:
   ```
   POST https://MNRWEFSS2Q-dsn.algolia.net/1/indexes/grailed_all/query
   ```
3. If the Algolia key can't be extracted, falls back to fetching the Grailed search page HTML and parsing `__NEXT_DATA__` JSON
4. Filters by `price_i <= max_usd` at the Algolia level before parsing

**Notes:**
- Grailed's Algolia API key is public-facing (embedded in their page for the browser to use) but does rotate occasionally. If Grailed scraping stops returning results, the key extraction regex in `api.py` may need updating.
- The Algolia App ID (`MNRWEFSS2Q`) is stable and does not change.

### Depop

Uses `StealthyFetcher` to render `depop.com/search/?q={keyword}` and extracts product data from the `__NEXT_DATA__` script tag embedded in the rendered HTML.

**Notes:**
- Depop's direct JSON API (`webapi.depop.com`) returns 403 for unauthenticated requests without proper session headers. The Next.js page approach bypasses this.

### eBay AU

Uses `StealthyFetcher` to render the eBay search results page, then extracts listings using regex against the rendered HTML.

**Notes:**
- eBay AU blocks basic HTTP requests (returns "Access Denied"). The stealth browser bypasses this.
- Filtering by price is done via the `_udhi` URL parameter before scraping to reduce result set size.

---

## Response Time

| Site | Typical time |
|---|---|
| Grailed (Algolia path) | 5–10 seconds |
| Grailed (HTML fallback) | 20–35 seconds |
| Depop | 20–35 seconds |
| eBay AU | 20–35 seconds |

Render free tier adds ~30 seconds on first request after idle (cold start).  
The frontend sets a 90-second timeout to handle this.

---

## Adding a New Site

1. Write a new scraper function in `api.py` following this pattern:

```python
def scrape_mysite(keyword: str, max_usd: float) -> list:
    max_aud = round(max_usd * USD_TO_AUD)
    url = f"https://mysite.com/search?q={urllib.parse.quote_plus(keyword)}"
    
    try:
        html = stealth_get(url)
        # ... parse html, extract items ...
        out = []
        for item in parsed_items:
            out.append({
                "brand": "...",
                "item": "...",
                "desc": "...",
                "size": "...",
                "origPrice": "...",
                "aud": 0,
                "under": False,
                "site": "MySite",
                "url": "...",
                "rep": "auth",
                "notes": "Live MySite listing",
                "image": "...",
            })
        return out[:12]
    except Exception as e:
        print(f"MySite error: {e}")
        return []
```

2. Register it in the `SCRAPERS` dict:

```python
SCRAPERS = {
    "grailed": scrape_grailed,
    "depop": scrape_depop,
    "ebay": scrape_ebay,
    "mysite": scrape_mysite,   # ← add this
}
```

3. Add the option to the dropdown in `index.html`:

```html
<select id="scrapeSite">
  ...
  <option value="mysite">MySite</option>
</select>
```

4. Push both files and redeploy on Render (auto-deploys on git push if connected).
