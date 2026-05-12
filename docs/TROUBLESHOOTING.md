# Troubleshooting

---

## Site Issues

### "No listings loaded yet" on the main grid

**Cause:** `data.json` doesn't exist yet in the repo. The GitHub Action hasn't run.

**Fix:**  
1. Repo → Actions → Scrape Clothing Data → Run workflow  
2. Wait 3 minutes  
3. Hard refresh the site (Ctrl+Shift+R)

---

### Main grid is stuck showing old data / not updating

**Cause:** The GitHub Action is either not running or running but finding 0 results.

**Fix:**
1. Check Actions tab — is the workflow running on schedule?
2. Click the latest run → expand "Run scraper" → look for error messages
3. If you see `Got 0 items` for all brands, Grailed may have changed its HTML structure  
   → See `docs/SCRAPER.md` — "Keeping the Scraper Working Long-Term"

---

### "Last updated" shows a very old date

Same as above — the scraper ran but wrote the same `data.json`. Check GitHub Actions logs.

---

## Scrape Button Issues

### "Setup needed: Deploy the API to Render first..."

**Cause:** The `SCRAPE_API` constant in `index.html` still says `YOUR-APP` or `localhost:8001`.

**Fix:**  
1. Deploy to Render (see `docs/SETUP.md` Step 3)  
2. Replace the URL in `index.html` → push → done

---

### Scrape button shows a spinner forever / times out

**Cause:** One of:
- Render server is cold-starting (first request after 15 min idle) — wait 30s and try again
- Render server crashed — check Render dashboard logs
- The target site blocked the scraper IP

**Fix:**
1. Go to render.com → your service → check "Logs" tab for errors
2. If it's a cold start, clicking Scrape again usually works
3. If the site is blocking: try a different site from the dropdown

---

### Scrape returns "No results found" for a keyword that should have results

**Cause:** One of:
- Price filter is too low (e.g. $100 AUD for BAPE is extremely cheap)
- The keyword is too specific
- Grailed's Algolia key extraction failed — API fell back to HTML scrape which got blocked

**Fix:**
1. Try raising the max price
2. Try a broader keyword (e.g. "bape" instead of "bape shark hoodie sz m")
3. Try a different site (Depop or eBay may have it when Grailed doesn't)
4. Check Render logs for `Algolia error` or `Grailed HTML fallback error`

---

### Scrape button works locally but not on the live site

**Cause:** `SCRAPE_API` in `index.html` points to `localhost:8001` which only works on your machine.

**Fix:** Change it to your Render URL and push (see `docs/SETUP.md` Step 4).

---

## Local Development Issues

### `ModuleNotFoundError: No module named 'playwright'`

```powershell
pip install playwright
python -m playwright install chromium
```

---

### `ModuleNotFoundError: No module named 'curl_cffi'`

```powershell
pip install curl_cffi
```

---

### `ModuleNotFoundError: No module named 'browserforge'`

```powershell
pip install scrapling[all]
```

---

### API server won't start — port 8001 already in use

```powershell
# Find what's using port 8001
netstat -ano | findstr :8001

# Kill the process (replace 1234 with the PID from above)
taskkill /PID 1234 /F
```

---

### Scrapling not installed correctly — keeps throwing import errors

Full clean install:
```powershell
pip uninstall scrapling -y
pip install "scrapling[all]"
python -m playwright install chromium
scrapling install camoufox --headless
```

---

## GitHub Actions Issues

### Action fails at "Install Camoufox browser" step

**Cause:** `scrapling install camoufox --headless` command changed or isn't available.

**Fix:** Update `.github/workflows/scrape.yml`. Try:
```yaml
- name: Install Camoufox browser
  run: python -m camoufox fetch
```

---

### Action fails with `git push` permission error

**Cause:** The workflow doesn't have write permissions to the repo.

**Fix:** In the workflow file, make sure this is present:
```yaml
permissions:
  contents: write
```
It's already in `scrape.yml` — if it's missing, add it back.

---

### Action runs but commits nothing even when scraper found items

**Cause:** `data.json` content is identical to what was already committed (same listings found twice).

This is normal behaviour — the `git diff --staged --quiet` check prevents pointless commits.  
It's not a bug.

---

## Render Issues

### Render deploy fails at the build step

Check the build logs on Render. Common causes:

| Error | Fix |
|---|---|
| `pip install` fails | Check `requirements.txt` — make sure `scrapling[camoufox]` is listed |
| `scrapling install camoufox` fails | Add `python-camoufox` to requirements.txt and remove the `scrapling install` command |
| Out of memory during build | Upgrade to Render paid tier or optimize the build |

---

### Render service keeps restarting (crash loop)

1. Open Render dashboard → your service → Logs
2. Look for the Python traceback
3. Most common cause: an import error on startup — fix the import in `api.py` and push

---

### Render URL returns 404

**Cause:** The service name or URL changed, or deploy failed.

**Fix:**
1. Go to render.com → open your service
2. Copy the current URL from the dashboard
3. Update `SCRAPE_API` in `index.html`

---

## General Debugging Commands

```powershell
# Test if the API is up
Invoke-WebRequest -Uri "http://localhost:8001/health" -UseBasicParsing | Select -Expand Content

# Test a live scrape (Grailed, bape, $500 max)
Invoke-WebRequest -Uri "http://localhost:8001/scrape?keyword=bape&site=grailed&max_price=500" -UseBasicParsing | Select -Expand Content

# Check Python packages
pip list | findstr -i "scrapling fastapi uvicorn playwright"

# Check if port 8001 is in use
netstat -ano | findstr :8001

# Check if port 8080 (website) is in use
netstat -ano | findstr :8080
```
