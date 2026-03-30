# Job Search Bot

Automated job crawler, ranker, and applicator. Crawls Greenhouse, Lever, and LinkedIn daily, ranks jobs by fit, and auto-applies via Playwright.

## Setup

```bash
python -m venv .venv
.venv/bin/pip install requests pandas pyyaml playwright python-dotenv openai
.venv/bin/playwright install chromium
```

Create `.env`:
```
LINKEDIN_EMAIL=...
LINKEDIN_PASSWORD=...
OPENAI_API_KEY=...   # optional, for AI-assisted form filling
```

Edit `apply/profile.yaml` with your personal info, work authorization, and resume path.

## Daily workflow

```bash
.venv/bin/python crawler/crawler.py          # Greenhouse + Lever → jobs.csv
.venv/bin/python crawler/crawler_linkedin.py # LinkedIn → linkedin_jobs.csv
.venv/bin/python rank/ranker.py              # score + rank → ranked_jobs.csv (top 40)
.venv/bin/python apply/applicator.py        # auto-apply in rank order
```

## How it works

### Crawlers

**`crawler/crawler.py`** — calls public Greenhouse and Lever JSON APIs (no auth needed). Reads `companies.yaml` for the list of companies to check.

**`crawler/crawler_linkedin.py`** — uses Playwright to scrape LinkedIn job search pages.

Both crawlers apply the same filters before saving:
- Title must match ML/AI/SWE/data roles
- Location must be Canada or remote (USA included as fallback)
- Entry-level only (no senior, staff, principal, director, manager, VP, lead, intern)
- No 6+ years experience or PhD requirements in the description
- Posted within last 24h
- Company not in blocklist (Mercor, DataAnnotation, Outlier, Appen, etc.)

### Ranker (`rank/ranker.py`)

Merges both CSVs, removes already-applied jobs, dedupes by `company + title`, and scores each job:

| Signal | Max pts | Logic |
|---|---|---|
| Title match | 35 | ML/AI engineer > data scientist > SWE |
| Company tier | 25 | Tier 1 (Google/OpenAI/Anthropic) > Tier 2 (Stripe/Shopify) > other |
| Location | 20 | Canada city/province > remote > US-only |
| Recency | 15 | <6h > <12h > <24h > older |
| Description keywords | 10 | LLM, RAG, PyTorch, agents, transformers, etc. |

Outputs `ranked_jobs.csv` with top 40. Add companies to `COMPANY_BLOCKLIST` or locations to `INELIGIBLE_LOCATION_TERMS` in the ranker to filter more aggressively.

### Applicator (`apply/applicator.py`)

Reads `ranked_jobs.csv` in rank order and auto-applies via Playwright. Detects ATS type (Greenhouse, Lever, LinkedIn Easy Apply, or generic fallback). Logs every attempt to `apply/applied_log.csv` — already-applied URLs are skipped on future runs.

## Files

```
companies.yaml          # Greenhouse tokens + Lever handles to crawl
apply/profile.yaml      # your info, work auth, resume path, Q&A answers
apply/applied_log.csv   # auto-generated log of all application attempts
jobs.csv                # crawler.py output
linkedin_jobs.csv       # crawler_linkedin.py output
ranked_jobs.csv         # ranker output (top 40, apply these)
resume/                 # resume PDFs (Agentic AI, Data Science, Modeling)
```
