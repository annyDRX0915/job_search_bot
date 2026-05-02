# Job Search Bot — Google Sheets Migration + Email Pipeline

## Status: Phase 4 in progress

---

## Architecture Decision

All CSV state moves to a single Google Spreadsheet with one tab per file:

| Tab name        | Replaces                  | Written by                    | Read by                           |
|-----------------|---------------------------|-------------------------------|-----------------------------------|
| `jobs`          | `jobs.csv`                | `crawler/crawler.py`          | `rank/ranker.py`                  |
| `linkedin_jobs` | `linkedin_jobs.csv`       | `crawler/crawler_linkedin.py` | `rank/ranker.py`                  |
| `email_jobs`    | _(new)_                   | `crawler/crawler_email.py`    | `rank/ranker.py`                  |
| `ranked_jobs`   | `ranked_jobs.csv`         | `rank/ranker.py`              | `apply/applicator.py`             |
| `applied_log`   | `apply/applied_log.csv`   | `apply/applicator.py`         | `rank/ranker.py`, `applicator.py` |

**Auth:** Service account (no OAuth flow — just a JSON key stored as a GitHub secret).  
**Local fallback:** If `SPREADSHEET_ID` is not set, scripts fall back to local CSVs.

---

## Phase 1 — One-time Setup ✅

### Google Cloud
- [x] Go to console.cloud.google.com → create or reuse a project
- [x] Enable **Google Sheets API** (same project as Gmail)
- [x] Go to IAM → Service Accounts → Create service account (name it `job-bot`)
- [x] Create a JSON key for it → download it (keep it secret, do not commit)
- [x] Copy the service account email (looks like `job-bot@your-project.iam.gserviceaccount.com`)

### Google Sheets
- [x] Create a new Google Spreadsheet (can be blank)
- [x] Share it with the service account email above (Editor access)
- [x] Copy the Spreadsheet ID from the URL:
      `https://docs.google.com/spreadsheets/d/<SPREADSHEET_ID>/edit`

### GitHub Secrets (Settings → Secrets → Actions)
- [ ] `SPREADSHEET_ID` — the ID copied above
- [ ] `GOOGLE_SERVICE_ACCOUNT_JSON` — paste the full contents of the downloaded JSON key file
- [ ] `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN` — for email crawler (Phase 4)
- [ ] `DISCORD_WEBHOOK_URL` — for Discord notifications (Phase 4)

### Local `.env`
- [x] Add `SPREADSHEET_ID=...`
- [x] Add `GOOGLE_SERVICE_ACCOUNT_JSON_PATH=/path/to/key.json` (file path instead of inline JSON)

---

## Phase 2 — Sheets Helper (`utils/gsheets.py`) ✅

- [x] Create `utils/__init__.py` (empty)
- [x] Create `utils/gsheets.py` with three functions:
  - [x] `read_sheet(tab: str) -> pd.DataFrame`
  - [x] `write_sheet(tab: str, df: pd.DataFrame)`
  - [x] `append_rows(tab: str, rows: list[dict])`
- [x] Auth: reads `GOOGLE_SERVICE_ACCOUNT_JSON` or `GOOGLE_SERVICE_ACCOUNT_JSON_PATH`
- [x] If `SPREADSHEET_ID` not set → raise a clear error

---

## Phase 3 — Migrate Existing Scripts ✅

### `crawler/crawler.py`
- [x] Replace `df.to_csv("jobs.csv", ...)` → `write_sheet("jobs", df)`
- [x] CSV fallback
- [x] `strip_html()` on description field (Greenhouse returns raw HTML)
- [x] `job_key` hash added to each job

### `crawler/crawler_linkedin.py`
- [x] Replace `df.to_csv("linkedin_jobs.csv", ...)` → `write_sheet("linkedin_jobs", df)`
- [x] CSV fallback
- [x] `job_key` hash added

### `rank/ranker.py`
- [x] `load_jobs()`: reads `jobs`, `linkedin_jobs`, `email_jobs` sheets (CSV fallback)
- [x] `load_applied()`: reads `applied_log` sheet (CSV fallback)
- [x] `main()`: writes `ranked_jobs` sheet (CSV fallback)
- [x] AI re-ranking via OpenAI `gpt-4o-mini` after rule-based pre-filter
- [x] `job_key` used for deduplication

### `apply/applicator.py`
- [x] `load_jobs()`: reads `ranked_jobs` sheet (CSV fallback)
- [x] `load_applied()`: reads `applied_log` sheet (CSV fallback)
- [x] `log_result()`: appends to `applied_log` sheet (CSV fallback)
- [x] `job_key` stored in log and checked for skip

---

## Phase 4 — Email Pipeline

### One-time Gmail auth (`scripts/gmail_auth.py`)
- [ ] Local OAuth2 flow to generate a refresh token
- [ ] Prints the refresh token to copy into GitHub Secrets

### Gmail crawler (`crawler/crawler_email.py`)
- [ ] Auth with Gmail API using refresh token
- [ ] Fetch emails from last 24h from `jobalerts-noreply@linkedin.com` and `alert@indeed.com`
- [ ] Parse job listings from HTML email body (title, company, location, url)
- [ ] Flag "important" emails (subject contains: interview, invitation, offer, next steps, assessment)
- [ ] Write parsed jobs → `write_sheet("email_jobs", df)`

### AI email agent (`agents/email_agent.py`)
- [ ] Accept list of flagged important email bodies
- [ ] Call OpenAI API → 3–5 bullet summary + recommended action per email
- [ ] Return structured summaries for the notifier

### Discord notifier (`notify/notifier.py`)
- [ ] Read top 10 rows from `ranked_jobs` sheet
- [ ] Send daily job digest to Discord (company, title, location, ai_score, url, ai_reason)
- [ ] Send important email summaries to Discord (AI summary per email)

### GitHub Actions workflow (`.github/workflows/daily_job_pipeline.yml`)
- [ ] Cron: `0 9 * * *` (9am UTC = 5am ET)
- [ ] Steps: checkout → install deps → run `crawler.py` → run `crawler_email.py` → run `ranker.py` → run `notifier.py`
- [ ] Load all secrets as env vars
- [ ] Note: `crawler_linkedin.py` runs locally only — writes to `linkedin_jobs` sheet
- [ ] Note: applicator is NOT automated (requires interactive browser + human confirmation)

---

## Phase 5 — Testing

- [x] `utils/gsheets.py` standalone — read/write/append confirmed working
- [x] `crawler/crawler.py` locally — `jobs` tab populates in Sheets
- [ ] `crawler/crawler_linkedin.py` locally — confirm `linkedin_jobs` tab populates
- [ ] `rank/ranker.py` locally — confirm `ranked_jobs` tab populates, `applied_log` read correctly
- [ ] `apply/applicator.py` locally on one job — confirm `applied_log` tab gets a new row
- [ ] `scripts/gmail_auth.py` locally — confirm refresh token works
- [ ] `crawler/crawler_email.py` locally — confirm `email_jobs` tab populates
- [ ] `notify/notifier.py` locally — confirm Discord message arrives
- [ ] Push → trigger GitHub Actions manually → confirm full pipeline runs in cloud

---

## Phase 6 — Review

_Fill in after completion_

- [ ] All tabs populate correctly after a full run
- [ ] `applied_log` persists correctly across GitHub Actions runs
- [ ] Discord digest looks good (top 10 jobs, clean formatting)
- [ ] Important email summaries are useful
- [ ] CSV fallback still works locally without `SPREADSHEET_ID` set
