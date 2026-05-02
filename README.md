# Job Search Bot

Automated job crawler, AI ranker, and auto-applicator. Crawls Greenhouse, Lever, LinkedIn, and email job alerts daily, ranks jobs with GPT-4o-mini, and auto-applies via Playwright. All state lives in Google Sheets.

## Setup

```bash
python -m venv .venv
.venv/bin/pip install requests pandas pyyaml playwright python-dotenv openai \
    google-api-python-client google-auth-oauthlib gspread google-auth beautifulsoup4
.venv/bin/playwright install chromium
```

### Environment variables (`.env`)

```
# LinkedIn scraper
LINKEDIN_EMAIL=...
LINKEDIN_PASSWORD=...

# OpenAI (AI ranking + applicator form filling)
OPENAI_API_KEY=...

# Google Sheets — all state stored here
SPREADSHEET_ID=...
GOOGLE_SERVICE_ACCOUNT_JSON_PATH=/path/to/service-account-key.json

# Gmail crawler (email job alerts)
GMAIL_CLIENT_ID=...
GMAIL_CLIENT_SECRET=...
GMAIL_REFRESH_TOKEN=...   # generate once with: .venv/bin/python scripts/gmail_auth.py
```

Edit `apply/profile.yaml` with your personal info, work authorization, and resume path.

## Daily workflow

```bash
# Runs locally — writes to Google Sheets
.venv/bin/python crawler/crawler.py          # Greenhouse + Lever → Sheets:jobs
.venv/bin/python crawler/crawler_linkedin.py # LinkedIn scrape → Sheets:linkedin_jobs
.venv/bin/python crawler/crawler_email.py    # Gmail job alerts → Sheets:email_jobs
.venv/bin/python rank/ranker.py              # AI rank → Sheets:ranked_jobs (top 40)
chrome-bot                                   # open bot Chrome (see setup below)
.venv/bin/python apply/applicator.py        # auto-apply in rank order (interactive)
```

`crawler.py` and `crawler_email.py` also run automatically every day via GitHub Actions (9 am UTC). `crawler_linkedin.py` and `apply/applicator.py` are local-only (require a real browser session).

## Applicator setup (CDP mode)

The applicator connects to your real Chrome browser via CDP so it reuses your existing login sessions (LinkedIn, Workday, Indeed, etc.) and avoids bot detection.

### One-time setup

**1. Create a dedicated Chrome profile for the bot:**

```bash
mkdir -p "$HOME/chrome-cdp-profile"
```

**2. Add a `chrome-bot` alias to `~/.zshrc`:**

```bash
echo 'alias chrome-bot="/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --user-data-dir=$HOME/chrome-cdp-profile"' >> ~/.zshrc
source ~/.zshrc
```

**3. Launch the bot Chrome and log in once:**

```bash
chrome-bot
```

In the window that opens, log into LinkedIn (and any other job sites you use). These sessions are saved in `~/chrome-cdp-profile` and persist across runs.

### Daily usage

```bash
chrome-bot                                 # open bot Chrome (keep this terminal open)
.venv/bin/python apply/applicator.py      # connect via CDP and start applying
```

Your normal Chrome (opened from Dock/Spotlight) is completely unaffected — both windows run simultaneously.

### Flags

```bash
.venv/bin/python apply/applicator.py --no-cdp    # fall back to fresh browser + LinkedIn login
.venv/bin/python apply/applicator.py --no-ai     # skip AI answers, rule-based only
.venv/bin/python apply/applicator.py --cdp-url http://localhost:9222  # custom CDP endpoint
```

### How it works

- **Greenhouse / Lever / LinkedIn Easy Apply**: fully automated — bot fills and you confirm submit
- **Workday / Indeed / Taleo / other login-required ATSs**: bot navigates to the page (you're already logged in), pre-fills name/email/phone/resume, then hands off — you fill the rest and submit
- **Bot/CAPTCHA detected**: falls back to same pre-fill + hand-off flow
- For every job: `Enter` = applied, `n` = next page (bot fills again), `s` = skip, `q` = quit

## How it works

### Crawlers

**`crawler/crawler.py`** — calls public Greenhouse and Lever JSON APIs (no auth needed). Reads `companies.yaml` for the list of companies.

**`crawler/crawler_linkedin.py`** — Playwright scrape of LinkedIn job search pages. Requires `LINKEDIN_EMAIL` / `LINKEDIN_PASSWORD`.

**`crawler/crawler_email.py`** — fetches LinkedIn, Indeed, and Glassdoor job alert emails from Gmail (last 24h), parses job listings, fetches full descriptions, and filters them. Supports:
- `jobalerts-noreply@linkedin.com` — LinkedIn job alerts
- `donotreply@jobalert.indeed.com` — Indeed job alert digests
- `donotreply@match.indeed.com` — Indeed single job match emails
- `noreply@glassdoor.com` — Glassdoor job alerts

All three crawlers apply the same filters:
- Title must match ML / AI / SWE / data roles
- Entry-level only — no senior, staff, principal, director, manager, VP, lead
- No 5+ years experience or PhD requirements (checked in description when available)
- Company not in blocklist (Mercor, DataAnnotation, Outlier, Appen, Scale AI, etc.)

### Ranker (`rank/ranker.py`)

Merges all three job sheets, removes already-applied jobs, dedupes by `job_key` (MD5 of title + company + location), and scores each job:

| Signal | Max pts | Logic |
|---|---|---|
| Title match | 35 | ML/AI engineer > data scientist > SWE |
| Company tier | 25 | Tier 1 (Google/OpenAI/Anthropic) > Tier 2 (Stripe/Shopify) > other |
| Location | 20 | Canada city/province > remote > US-only |
| Recency | 15 | <6h > <12h > <24h > older |
| Description keywords | 10 | LLM, RAG, PyTorch, agents, transformers, etc. |

Top 50 rule-based results are then re-ranked by **GPT-4o-mini**, which considers your applied/skipped history to surface better matches. Outputs top 40 to `Sheets:ranked_jobs`.

### Applicator (`apply/applicator.py`)

Reads `Sheets:ranked_jobs` in rank order and auto-applies via Playwright. Detects ATS type (Greenhouse, Lever, LinkedIn Easy Apply, or generic fallback). Logs every attempt to `Sheets:applied_log` — already-applied jobs are skipped on future runs.

## Google Sheets tabs

| Tab | Written by | Read by |
|---|---|---|
| `jobs` | `crawler.py` | `ranker.py` |
| `linkedin_jobs` | `crawler_linkedin.py` | `ranker.py` |
| `email_jobs` | `crawler_email.py` | `ranker.py` |
| `ranked_jobs` | `ranker.py` | `applicator.py` |
| `applied_log` | `applicator.py` | `ranker.py`, `applicator.py` |

All scripts fall back to local CSVs if `SPREADSHEET_ID` is not set.

## Files

```
companies.yaml             # Greenhouse tokens + Lever handles to crawl
apply/profile.yaml         # your info, work auth, resume path, Q&A answers
scripts/gmail_auth.py      # one-time Gmail OAuth2 setup → prints GMAIL_REFRESH_TOKEN
resume/                    # resume PDFs (Agentic AI, Data Science, Modeling)
utils/gsheets.py           # Google Sheets helper (read_sheet / write_sheet / append_rows)
```
