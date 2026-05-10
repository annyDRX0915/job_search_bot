# Job Search Bot — Google Sheets Migration + Email Pipeline

## Status: Phase 4 done ✅ — Phase 5 CI test passed, notifier optional

---

## Architecture Decision

All CSV state moves to a single Google Spreadsheet with one tab per file:

| Tab name        | Replaces                  | Written by                    | Read by                           |
|-----------------|---------------------------|-------------------------------|-----------------------------------|
| `jobs`          | `jobs.csv`                | `crawler/crawler.py`          | `rank/ranker.py`                  |
| `linkedin_jobs` | `linkedin_jobs.csv`       | `crawler/crawler_linkedin.py` | `rank/ranker.py`                  |
| `email_jobs`    | _(new)_                   | `crawler/crawler_email.py`    | `rank/ranker.py`                  |
| `ranked_jobs`   | `ranked_jobs.csv`         | `rank/ranker.py`              | `apply/applicator.py`             |
| `applied_log`   | `apply/applied_log.csv`   | `apply/applicator.py`, `agents/email_agent.py` | `rank/ranker.py`, `applicator.py` |

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
- [x] Share it with the service account email above (Editor accesspush)
- [x] Copy the Spreadsheet ID from the URL:
      `https://docs.google.com/spreadsheets/d/<SPREADSHEET_ID>/edit`

### GitHub Secrets (Settings → Secrets → Actions)
- [x] `SPREADSHEET_ID` — the ID copied above
- [x] `GOOGLE_SERVICE_ACCOUNT_JSON` — paste the full contents of the downloaded JSON key file
- [x] `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN` — for email crawler (Phase 4)
- [x] `DISCORD_WEBHOOK_URL` — for Discord notifications (Phase 4)

### Local `.env`
- [x] Add `SPREADSHEET_ID=...`
- [x] Add `GOOGLE_SERVICE_ACCOUNT_JSON_PATH=/path/to/key.json` (file path instead of inline JSON)

---

## Phase 2 — Sheets Helper (`utils/gsheets.py`)

- [x] Create `utils/__init__.py` (empty)
- [x] Create `utils/gsheets.py` with three functions:
  - [x] `read_sheet(tab: str) -> pd.DataFrame`
  - [x] `write_sheet(tab: str, df: pd.DataFrame)`
  - [x] `append_rows(tab: str, rows: list[dict])`
- [x] Auth: reads `GOOGLE_SERVICE_ACCOUNT_JSON` or `GOOGLE_SERVICE_ACCOUNT_JSON_PATH`
- [x] If `SPREADSHEET_ID` not set → raise a clear error
- [x] Add `upsert_rows(tab: str, key_col: str, rows: list[dict])` — superseded: email agent uses read → update in-memory → write_sheet instead

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
- [x] `load_applied()`: handle new schema — job is "seen" if any row exists; handle rows missing `interviewed`/`rejected` columns gracefully (old CSV)

### `apply/applicator.py`
- [x] `load_jobs()`: reads `ranked_jobs` sheet (CSV fallback)
- [x] `load_applied()`: reads `applied_log` sheet (CSV fallback)
- [x] `log_result()`: appends to `applied_log` sheet (CSV fallback)
- [x] `job_key` stored in log and checked for skip
- [x] `LOG_FIELDS` updated to include `interviewed` and `rejected` columns (empty on new applications)
- [x] `load_applied()`: skip job if row exists with any non-empty value in `status`, `interviewed`, or `rejected` (handle old CSV rows without these columns)

---

## Phase 4 — Email Pipeline

### One-time Gmail auth (`scripts/gmail_auth.py`)
- [x] Local OAuth2 flow to generate a refresh token
- [x] Prints the refresh token to copy into GitHub Secrets

### Gmail crawler (`crawler/crawler_email.py`)
- [x] Auth with Gmail API using refresh token
- [x] Fetch emails from last 24h from `jobalerts-noreply@linkedin.com` and `alert@indeed.com`
- [x] Parse job listings from HTML email body (title, company, location, url)
- [x] Flag "important" emails — handled by `email_agent.py` AI triage instead
- [x] Write parsed jobs → `write_sheet("email_jobs", df)`

### Applied log schema (`applied_log` sheet) ✅
Final schema (already migrated):
`job_key | url | company | title | source | status | interviewed | rejected | timestamp | notes`
- `status`: applied / skipped / failed (written by applicator)
- `interviewed`: "interviewed" or empty (written by email agent)
- `rejected`: "rejected" or empty (written by email agent)
Old rows keep their existing values — migration scripts already ran.

### AI email agent (`agents/email_agent.py`) ✅
Three-pass pipeline over **all** emails from last 24h:

**Pass 1 — Static spam pre-filter (free, no tokens)**
- [x] Load `agents/spam_senders.json` — known spam domains + subject keyword patterns
- [x] Drop job alert senders already handled by `crawler_email.py` (no tokens wasted)
- [x] Drop emails matching known spam senders or subject patterns before any AI call

**Pass 2 — AI triage (batch call, subjects + senders + snippets only)**
- [x] Send surviving emails to `gpt-4o-mini` in one call
- [x] Model returns category per email: `interview`, `rejection`, `offer`, `job_application`, `important`, `spam`, `newsletter`, `notification`
- [x] Append any newly AI-identified spam senders back to `agents/spam_senders.json`
- [x] Discard `spam` / `newsletter` / `notification` after categorization

**Pass 3 — AI summarize (targeted, full body)**
- [x] For `interview`, `rejection`, `offer`, `important`, `job_application`: fetch full email body
- [x] Per-email call → 1–2 sentence summary + recommended action
- [x] Post-summarize reclassification: summaries containing rejection phrases upgrade `important` → `rejection`

**Applied log updates**
- [x] For `interview` emails: find matching row in `applied_log` by `company + title` fuzzy match → set `interviewed` = "interviewed"
- [x] For `rejection` emails: find matching row → set `rejected` = "rejected"
- [x] Uses read → update in-memory → write_sheet (upsert_rows not needed)
- [x] Logs when no match found in applied_log

**Output**
- [x] Build structured digest: ACTION REQUIRED → OFFERS → REJECTIONS → APPLICATION UPDATES (capped at 5) → OTHER IMPORTANT → filtered count with breakdown
- [x] Post digest to Discord via `DISCORD_WEBHOOK_URL` (chunked into ≤1900 char messages)
- [x] Seed file: `agents/spam_senders.json` with common spam/newsletter domains

### Discord notifier (`notify/notifier.py`)
- [ ] Read top 10 rows from `ranked_jobs` sheet
- [ ] Send daily job digest to Discord (company, title, location, ai_score, url, ai_reason)
- [ ] Send important email summaries to Discord (AI summary per email)

### GitHub Actions workflow (`.github/workflows/daily_job_pipeline.yml`)
- [x] Cron: runs twice daily at 9am and 12pm ET
- [x] Steps: checkout → install deps → run `crawler.py` → run `crawler_email.py` → run `ranker.py` → run `email_agent.py`
- [x] Load all secrets as env vars
- [x] Note: `crawler_linkedin.py` runs locally only — writes to `linkedin_jobs` sheet
- [x] Note: applicator is NOT automated (requires interactive browser + human confirmation)

---

## Phase 5 — Testing

- [x] `utils/gsheets.py` standalone — read/write/append confirmed working
- [x] `crawler/crawler.py` locally — `jobs` tab populates in Sheets
- [x] `crawler/crawler_linkedin.py` locally — confirm `linkedin_jobs` tab populates
- [x] `rank/ranker.py` locally — confirm `ranked_jobs` tab populates, `applied_log` read correctly
- [x] `apply/applicator.py` locally on one job — confirm `applied_log` tab gets a new row
- [x] `scripts/gmail_auth.py` locally — confirm refresh token works
- [x] `crawler/crawler_email.py` locally — confirm `email_jobs` tab populates
- [x] `utils/gsheets.py` `upsert_rows()` — superseded, not needed
- [x] `apply/applicator.py` locally — confirm new row in `applied_log` has `interviewed` and `rejected` columns (empty)
- [x] `agents/email_agent.py` locally — confirm `interviewed`/`rejected` columns update correctly on matched rows
- [x] `agents/email_agent.py` locally — confirm Discord digest arrives with correct sections
- [x] `agents/spam_senders.json` — confirm newly flagged senders are appended after a run
- [x] `notify/notifier.py` — superseded: Discord digest handled directly by `email_agent.py`
- [x] Push → trigger GitHub Actions manually → confirm full pipeline runs in cloud

---

## Phase 6 — Review

_Fill in after completion_

- [ ] All tabs populate correctly after a full run
- [ ] `applied_log` persists correctly across GitHub Actions runs
- [ ] Discord digest looks good (top 10 jobs, clean formatting)
- [ ] Important email summaries are useful
- [ ] CSV fallback still works locally without `SPREADSHEET_ID` set

---

## Phase 8 — Email Memory, De-duplication & Apple Calendar ✅

### New file: `agents/email_store.py` (Google Sheets-backed memory)
- [x] `load_seen_ids()` → set of already-processed Gmail message IDs from `email_memory` sheet
- [x] `get_company_history(company, n=5)` → recent records for RAG context injection
- [x] `save_records(records)` → append new processed emails to `email_memory` sheet
- [x] Schema: `id | processed_at | category | company | title | summary | action | calendar_event_id | event_datetime`
- [x] Add `email_memory` tab to architecture table in README

### De-duplication
- [x] In `email_agent.py` `main()`: load seen IDs at startup, filter before any API calls
- [x] Verify: running agent twice in 24h doesn't double-post to Discord

### RAG context injection
- [x] In `summarize_email()`: inject `get_company_history()` results into prompt for `interview`/`offer` emails
- [x] Example: *"Previous emails from Stripe: rejection received 2026-04-20"*

### Extended summarize — event extraction
- [x] Add event fields to `summarize_email()` AI prompt for `interview` + `important` (assessments):
  - `event_type`: `"interview"` | `"assessment_deadline"` | `null`
  - `event_datetime`: ISO8601 or `null`
  - `event_duration_minutes`: integer (default 60)
  - `event_title`: display string for calendar
- [x] Handle parse failures gracefully: `null` → skip calendar, note in Discord message

### Apple Calendar (iCloud CalDAV)
- [x] Add `caldav` + `icalendar` to dependencies
- [x] Add `ICLOUD_USERNAME` + `ICLOUD_APP_PASSWORD` to `.env` and GitHub Secrets
- [x] Add `assessment_reminder_hours: 24` and `calendar_name: Job_Search` to `filters.yaml`
- [x] `_caldav_calendar()` helper — connects to `caldav.icloud.com`, finds calendar by name
- [x] `_create_apple_event(cal, title, start_dt, duration_min, description)` → returns UID
- [x] Interview emails: event at `event_datetime`, 60 min
- [x] Assessment deadline emails: event at `deadline − assessment_reminder_hours`
- [x] Idempotent: skip if `calendar_event_id` already set in store
- [x] Fix: mask ATS sender domains (myworkday.com etc.) so AI extracts company from body
- [x] Fix: capture img alt text for logo-only company names in HTML emails

### Discord updates
- [x] Interview items: show `📅 Added to calendar` or `⚠️ No time found`
- [x] Assessment items: show `📅 Reminder set for [date]`

### Testing
- [x] Run agent twice — confirm second run posts nothing new to Discord
- [x] Send test interview email to self — confirm event appears in Apple Calendar (Job_Search)
- [x] Send test assessment email — confirm event placed 24h before deadline
- [x] Check `email_memory` sheet has correct records after run

---

## Phase 7 — Chrome Extension (idea)

A lightweight Chrome extension that sits alongside the applicator:

**Core idea**: When the user navigates to a job application page, the extension detects the ATS and pre-fills fields using profile data — no Playwright needed.

### How it works
- Extension background script reads `profile.yaml` (or a JSON copy of it) from a local HTTP server the bot spins up
- Content script detects ATS type (Greenhouse, Lever, Workday, etc.) and injects pre-filled values
- User reviews the pre-filled form, clicks Next, and submits normally
- On submit, extension POSTs the result (company, title, url, status) back to the local server → logged to `applied_log`

### Components
- [ ] `extension/manifest.json` — MV3 manifest, permissions: `storage`, `activeTab`, host permissions for ATS domains
- [ ] `extension/background.js` — reads profile from local server, listens for tab events
- [ ] `extension/content.js` — detects ATS, fills form fields, logs results
- [ ] `extension/popup.html` + `popup.js` — shows profile status, pending jobs count, toggle on/off
- [ ] `scripts/profile_server.py` — tiny Flask/http.server that serves profile.yaml as JSON and accepts apply logs

### Advantages over Playwright
- Works with any ATS (user's real session, no bot detection)
- No browser launch needed — just browse normally
- User is always in control — extension only pre-fills, never auto-submits
- Works alongside CDP applicator (complementary, not a replacement)
