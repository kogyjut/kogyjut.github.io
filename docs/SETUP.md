# Setup Guide

Complete setup from zero to fully working site. Steps 1–2 are already done. Start at Step 3.

---

## Step 1 — GitHub Repo (DONE)

The repo `kogyjut/kogyjut.github.io` exists and all files are pushed.  
GitHub Pages is already enabled and serving the site at `kogyjut.github.io`.

---

## Step 2 — Local Development (DONE)

Dependencies already installed on this machine:

```
scrapling[all]
fastapi
uvicorn
requests
playwright
curl_cffi
```

To run the site locally:

```powershell
# Terminal 1 — website (serves index.html + data.json)
cd C:\Users\nathan\Desktop\kogyjut-update
python -m http.server 8080
# open http://localhost:8080

# Terminal 2 — scrape API
cd C:\Users\nathan\Desktop\kogyjut-update
uvicorn api:app --host 0.0.0.0 --port 8001
```

The `index.html` currently points to `http://localhost:8001` for the API.  
**This means the scrape button only works on your machine.**  
Complete Step 3 + 4 to make it work for everyone.

---

## Step 3 — Deploy API to Render (TODO)

This makes the scrape button work for all visitors.

1. Go to **render.com** and sign up with your GitHub account (free, no card needed)

2. Click **New +** → **Web Service**

3. Connect your GitHub account if prompted, then select the repo:  
   `kogyjut/kogyjut.github.io`

4. Render will auto-detect `render.yaml`. Settings should pre-fill as:
   - **Name:** `kogyjut-scraper-api`
   - **Runtime:** Python
   - **Build Command:** `pip install -r requirements.txt && scrapling install camoufox --headless`
   - **Start Command:** `uvicorn api:app --host 0.0.0.0 --port $PORT`

5. Click **Create Web Service**

6. Wait ~5 minutes for the first deploy (it downloads browser binaries)

7. Copy the URL Render gives you — looks like:  
   `https://kogyjut-scraper-api.onrender.com`

---

## Step 4 — Update the API URL in index.html (TODO)

After Step 3, open `index.html` and find this line near the bottom:

```javascript
const SCRAPE_API = 'http://localhost:8001';
```

Change it to your Render URL:

```javascript
const SCRAPE_API = 'https://kogyjut-scraper-api.onrender.com';
```

Then push the change:

```powershell
cd C:\Users\nathan\Desktop\kogyjut-repo
git add index.html
git commit -m "point SCRAPE_API to render"
git push
```

The live site at `kogyjut.github.io` will update in ~2 minutes.

---

## Step 5 — Run the GitHub Action Once (TODO)

The scheduled scraper runs every 8 hours automatically, but won't have run yet. Trigger it manually to generate the first `data.json`:

1. Go to `github.com/kogyjut/kogyjut.github.io`
2. Click the **Actions** tab
3. Click **Scrape Clothing Data** in the left sidebar
4. Click **Run workflow** → **Run workflow** (green button)
5. Wait ~3 minutes
6. Refresh your site — the main card grid should now show live Grailed listings

---

## Ongoing — Nothing Required

After Steps 3–5, everything runs itself:

- **Scraper** runs every 8 hours, updates `data.json` automatically
- **Render API** stays deployed, auto-restarts if it crashes
- **GitHub Pages** serves the latest `index.html` on every push

---

## Re-installing on a New Machine

```powershell
# Clone the repo
git clone https://github.com/kogyjut/kogyjut.github.io.git
cd kogyjut.github.io

# Install Python dependencies
pip install scrapling[all] fastapi uvicorn requests

# Install browser binaries
python -m playwright install chromium
scrapling install camoufox --headless

# Run locally
uvicorn api:app --host 0.0.0.0 --port 8001   # in one terminal
python -m http.server 8080                    # in another
```

---

## Environment Variables

No environment variables are required. The API runs without any API keys.

If you later want to add rate limiting or analytics, Render lets you set env vars from its dashboard under **Environment** in your service settings.
