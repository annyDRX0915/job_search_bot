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

# Apple Calendar (iCloud CalDAV — email agent)
ICLOUD_USERNAME=...           # your Apple ID email
ICLOUD_APP_PASSWORD=...       # app-specific password from appleid.apple.com → Security
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

`crawler.py` and `crawler_email.py` also run automatically every day via GitHub Actions (12:30 pm ET). `crawler_linkedin.py` and `apply/applicator.py` are local-only (require a real browser session).

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

All three crawlers apply the same filters — all defined in **`filters.yaml`** (edit there, no Python changes needed):
- Title must match ML / AI / SWE / data roles
- Entry-level only — no senior, staff, principal, director, manager, VP, lead
- No 5+ years experience or PhD requirements (checked in description when available)
- Company not in blocklist (Mercor, DataAnnotation, Outlier, Appen, Scale AI, etc.)
- Location: Canada, USA, Singapore, Malaysia, China, or remote

### Ranker (`rank/ranker.py`)

Merges all three job sheets, removes already-applied jobs, dedupes by `job_key` (MD5 of title + company + location), and scores each job:

| Signal | Max pts | Logic |
|---|---|---|
| Title match | 30 | ML/AI engineer > data scientist > SWE |
| Company tier | 25 | Tier 1 (Google/OpenAI/Anthropic) > Tier 2 (Stripe/Shopify/Grab) > other |
| Location | 20 | Canada (30) > Singapore/USA (25) > Malaysia (20) > China (18) > remote (15) |
| Recency | 15 | <6h > <12h > <24h > older |
| Description keywords | 10 | LLM, RAG, PyTorch, agents, transformers, etc. |

Top 50 rule-based results are then re-ranked by **GPT-4o-mini**, which considers your applied/skipped history to surface better matches. Outputs top 40 to `Sheets:ranked_jobs`.

### Applicator (`apply/applicator.py`)

Reads `Sheets:ranked_jobs` in rank order and auto-applies via Playwright. Detects ATS type (Greenhouse, Lever, LinkedIn Easy Apply, or generic fallback). Logs every attempt to `Sheets:applied_log` — already-applied jobs are skipped on future runs.

### Email agent (`agents/email_agent.py`)

Runs after the crawlers. Three-pass pipeline over all Gmail from the last 24h:

1. **De-duplication** — loads `Sheets:email_memory` and skips any email already processed in a previous run, so running twice a day never double-posts to Discord
2. **Static spam pre-filter** — drops job alert senders (already handled by `crawler_email.py`) and known spam domains (`agents/spam_senders.json`)
3. **AI triage** — batch call to GPT-4o-mini categorises every surviving email: `interview`, `rejection`, `offer`, `job_application`, `important`, `spam`, `newsletter`, `notification`
4. **AI summarise + event extraction** — for actionable emails, fetches the full body and asks the AI for a 1–2 sentence summary, recommended action, and (for interviews/assessments) the event datetime and title
5. **Apple Calendar** — creates events in your `Job_Search` iCloud calendar via CalDAV. Interview emails → event at the scheduled time. Assessment deadline emails (Codility, HackerRank, etc.) → reminder event 24h before the deadline. Calendar name and reminder window are configurable in `filters.yaml`
6. **Applied log update** — marks matching rows in `applied_log` as `interviewed` or `rejected`
7. **Persistent memory** — saves every processed email to `Sheets:email_memory` (id, category, company, title, summary, calendar event ID). Used as RAG context on future runs: if the same company emails again, the AI sees the history
8. **Discord digest** — posts a structured digest (ACTION REQUIRED → OFFERS → REJECTIONS → APPLICATION UPDATES → OTHER) with 📅/⚠️ calendar status on interview items

New env vars required: `ICLOUD_USERNAME`, `ICLOUD_APP_PASSWORD` (app-specific password from appleid.apple.com).

## Google Sheets tabs

| Tab | Written by | Read by |
|---|---|---|
| `jobs` | `crawler.py` | `ranker.py` |
| `linkedin_jobs` | `crawler_linkedin.py` | `ranker.py` |
| `email_jobs` | `crawler_email.py` | `ranker.py` |
| `ranked_jobs` | `ranker.py` | `applicator.py` |
| `applied_log` | `applicator.py` | `ranker.py`, `applicator.py`, `email_agent.py` |
| `email_memory` | `email_agent.py` | `email_agent.py` |

All scripts fall back to local CSVs if `SPREADSHEET_ID` is not set.

## Files

```
filters.yaml               # all job search filters in one place (locations, titles, companies, scoring, calendar)
companies.yaml             # Greenhouse tokens + Lever handles to crawl
apply/profile.yaml         # your info, work auth, resume path, Q&A answers
agents/email_store.py      # Google Sheets-backed email memory (de-dup, RAG, calendar tracking)
agents/spam_senders.json   # learned spam sender addresses (auto-updated each run)
scripts/gmail_auth.py      # one-time Gmail OAuth2 setup → prints GMAIL_REFRESH_TOKEN
utils/filters.py           # loads filters.yaml — imported by all crawlers and ranker
utils/gsheets.py           # Google Sheets helper (read_sheet / write_sheet / append_rows)
```

## Not included in this repo

These files contain personal info and are gitignored — you need to create them yourself:

| File | What it is |
|---|---|
| `.env` | API keys and credentials (see Environment variables above) |
| `apply/profile.yaml` | Your name, contact info, work authorization, resume path, and common Q&A answers |
| `apply/past_exp.txt` | Work experience text used for AI-generated answers |
| `resume/` | Your resume PDFs — place them anywhere and point `profile.yaml` at them |
| `service-account-key.json` | Google service account key for Sheets access (download from Google Cloud Console) |
