# cHAP Seller Tracker — Deploy Guide

Ship the dashboard to a public URL backed by an automated twice-daily scrape, with no laptop dependency.

## Architecture at a glance

```
 ┌──────────────────────────────────────────────────────────────────────┐
 │  GitHub (private repo)                                               │
 │  ┌──────────────────────────────────┐     ┌─────────────────────┐    │
 │  │  .github/workflows/scrape.yml    │     │  Admin Panel        │    │
 │  │  cron: 18:30 UTC + 06:30 UTC     │────►│  Scrapper/          │    │
 │  │  (= 12 AM + 12 PM IST daily)     │push │  results/           │    │
 │  └──────────────────────────────────┘     └──────────┬──────────┘    │
 │                                                       │               │
 └───────────────────────────────────────────────────────┼───────────────┘
                                                         │
                           push-to-main triggers redeploy│
                                                         ▼
 ┌──────────────────────────────────────────────────────────────────────┐
 │  Streamlit Community Cloud                                           │
 │  - reads dashboard.py                                                │
 │  - reads results/ committed by the Action                            │
 │  - serves at https://<app-slug>.streamlit.app                         │
 └────────────────────────────────────┬─────────────────────────────────┘
                                      │
               CNAME (dashboard.yourdomain.com → streamlit.app slug)
                                      ▼
 ┌──────────────────────────────────────────────────────────────────────┐
 │  Cloudflare DNS (+ optionally Cloudflare Proxy for caching/TLS)      │
 └──────────────────────────────────────────────────────────────────────┘
```

No Python runtime to manage. The scraper runs on GitHub's free Linux runners; the dashboard runs on Streamlit's free tier. Cloudflare gives you a custom domain + TLS.

## 1 — Push to a **private** GitHub repo

The scraped CSVs contain seller email addresses and store URLs. Customer data. Make the repo private.

```bash
cd "Admin Panel Scrapper"           # or wherever the project lives
git init -b main
git add .
git commit -m "init: cHAP scraper + dashboard"

# Create a PRIVATE repo on GitHub first (gh CLI shown here; the web UI works too)
gh repo create chap-seller-tracker --private --source=. --push
```

If the project is a subfolder of a larger repo, change the `working-directory` in `.github/workflows/scrape.yml` to match.

## 2 — Configure GitHub Actions secrets

Settings → Secrets and variables → Actions → **New repository secret**. Add each of the following; values are whatever you have in your local `.env`:

| Secret name | Example value |
|---|---|
| `LOGIN_URL` | `https://admin.cedcommerce.com/auth/login` |
| `APP_1_ID` | `shopify_temu` |
| `APP_1_USER` | `hrithik@threecolts.com` |
| `APP_1_PASS` | `…` |
| `APP_2_ID` | `shein` |
| `APP_2_USER` | `…` |
| `APP_2_PASS` | `…` |
| `APP_3_ID` | `shopify_temu_eu` |
| `APP_3_USER` | `…` |
| `APP_3_PASS` | `…` |
| `SUPABASE_URL` | (optional) `https://…supabase.co` |
| `SUPABASE_KEY` | (optional) service role key |

Commit nothing from your real `.env` file. `.env` is already in `.gitignore`; double-check with `git check-ignore .env`.

## 3 — Verify the workflow

Go to **Actions → scrape-chap → Run workflow** (the manual-run button). Pick the `main` branch. The run should:

1. Check out the repo.
2. Install Python 3.11 + `requirements.txt`.
3. Install Playwright Chromium.
4. Run `scraper.py`.
5. Commit `Admin Panel Scrapper/results/` back to `main`.
6. Log the per-app row + plan-populated counts.

A successful run leaves a commit like `chore(data): scrape 2026-04-19_18-30-00Z` on `main`. The two scheduled triggers kick in once that run has completed — GitHub honors the cron automatically.

### Cron schedule
GitHub Actions cron is UTC only. These two entries hit 12 AM and 12 PM India time:

```yaml
schedule:
  - cron: "30 18 * * *"   # 00:00 IST
  - cron: "30 6 * * *"    # 12:00 IST
```

No DST in India, so these stay correct year-round.

## 4 — Deploy the dashboard on Streamlit Community Cloud

1. Go to https://share.streamlit.io and sign in with GitHub.
2. Click **New app**.
3. Pick the private repo, branch `main`, main file path:
   - If this project is at the repo root: `dashboard.py`
   - If the folder is a subdirectory: `Admin Panel Scrapper/dashboard.py`
4. Advanced settings → **Python version**: 3.11.
5. Advanced settings → **Secrets**: paste a TOML block with anything the dashboard needs at runtime. For now the dashboard only reads files from `results/history/`, so secrets aren't strictly required — but this is a good place to add `SUPABASE_URL` / `SUPABASE_KEY` later if you wire Supabase-backed reads.
6. Click **Deploy**.

The first deploy takes a couple of minutes. Afterward each push to `main` (including the Action's scrape commits) triggers a redeploy in ~30 s — no manual intervention needed.

Your app URL looks like `https://chap-seller-tracker-<randomslug>.streamlit.app`.

## 5 — Point a Cloudflare domain at the Streamlit app

1. In Cloudflare, select your zone (the domain you want to use).
2. **DNS → Records → Add record**:
   - Type: `CNAME`
   - Name: `dashboard` (so the URL becomes `dashboard.yourdomain.com`)
   - Target: `<your-app-slug>.streamlit.app` (the whole hostname, no `https://`)
   - Proxy status: **DNS only (grey cloud)**. Start here; see the caveat below before flipping to orange-cloud proxy.
   - TTL: Auto.
3. In Streamlit Cloud → your app → **Settings → Custom subdomain/domain** → add `dashboard.yourdomain.com` and follow the verification prompt.

After verification, `https://dashboard.yourdomain.com` serves your dashboard with TLS automatically provisioned by Streamlit.

### Cloudflare Proxy caveat (important)
Streamlit uses WebSockets for its live rerun / widget update machinery. If you turn the orange cloud on (Cloudflare proxy mode), WebSockets will still work on a Pro plan (WebSocket support is enabled by default), but you must NOT enable aggressive caching for the dashboard hostname. Suggested Page Rule:

- URL: `dashboard.yourdomain.com/*`
- Settings: Cache Level = Bypass

On the free tier, leave it **DNS only** — you still get TLS from Streamlit and a clean custom hostname.

## 6 — Verify the end-to-end loop

1. Trigger a manual scrape: Actions → scrape-chap → Run workflow.
2. Wait ~3–5 minutes for the job to complete.
3. Check `git log --oneline -n 5` on the repo — you should see a new `chore(data): scrape …` commit.
4. Refresh `https://dashboard.yourdomain.com`. The "Last Updated" pill in the top header should show the fresh timestamp.

## Reference — local dev

Nothing in the above changes the local development flow:

```bash
cd "Admin Panel Scrapper"
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env                 # fill in real creds
python scraper.py                    # one scrape
streamlit run dashboard.py           # browse the dashboard
```

`HEADLESS=false` in local `.env` lets you watch Chromium work; production (Actions, Streamlit Cloud) keeps it `true`.

## Operational notes

- **Credentials change on the admin panel.** Update the GitHub secret and re-run the workflow; no code change needed.
- **Scraper fails with a login toast.** The Action uploads `debug_dom_*.txt` and `error_*.png` as a build artifact on failure — download it from the failed run's page, open the DOM dump, find the error, patch `scraper.py`, push. The next scheduled run picks up the fix automatically.
- **Want a third daily scrape?** Add another `- cron:` line under `schedule:` in `.github/workflows/scrape.yml` (UTC). E.g. for 18:00 IST add `- cron: "30 12 * * *"`.
- **Rolling back bad data.** `git revert <scrape commit>` on `main` pushes an earlier snapshot back to the top; Streamlit Cloud redeploys it and the dashboard rewinds.
