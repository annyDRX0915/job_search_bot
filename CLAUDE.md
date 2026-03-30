# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A job search bot with two crawlers and an auto-applicator:

- `crawler/crawler.py` — Fetches jobs from **Greenhouse** and **Lever** public APIs. Outputs `jobs.csv`.
- `crawler/crawler_linkedin.py` — Scrapes **LinkedIn** job search pages using Playwright. Outputs `linkedin_jobs.csv`.
- `rank/ranker.py` — Scores and ranks all crawled jobs, outputs `ranked_jobs.csv` (top 40).
- `apply/applicator.py` — Auto-applies to jobs in `ranked_jobs.csv` using Playwright.

## Daily workflow

```bash
.venv/bin/python crawler/crawler.py          # fetch Greenhouse + Lever jobs → jobs.csv
.venv/bin/python crawler/crawler_linkedin.py # scrape LinkedIn → linkedin_jobs.csv
.venv/bin/python rank/ranker.py              # score + rank → ranked_jobs.csv (top 40)
.venv/bin/python apply/applicator.py        # auto-apply in rank order
```

`crawler.py` reads `companies.yaml` (Greenhouse board tokens + Lever handles) from the working directory.

## Dependencies

```bash
pip install requests pandas pyyaml playwright python-dotenv
playwright install chromium
```

## Filters applied by both crawlers

- **Title relevance**: MLE, SWE, data scientist, AI engineer, etc.
- **Location**: Canada or USA only (including remote)
- **Entry-level**: excludes senior, staff, principal, director, manager, VP, lead, intern, co-op
- **Experience**: drops jobs requiring 6+ years or a PhD (from job description)
- **Recency**: posted within last 24h (`crawler.py` uses `updated_at`; LinkedIn uses `TIME_RANGE=r86400`)
- **Company blocklist**: excludes gig/task platforms (Mercor, DataAnnotation, Outlier, Appen, etc.)

## Resume files

Located in `resume/`:
- `resume/Resume_Agentic_AI/resume.pdf` — primary resume
- `resume/Resume_Data_Science/resume.pdf`
- `resume/Resume_Modeling/resume.pdf`

## Architecture

Both crawlers share the same output schema (`source`, `company`, `title`, `location`, `url`, `posted_at`, `description`) and a `dedupe_jobs()` function (deduplicates by URL first, then by `source + company + title + location`).

**`crawler.py`** calls public JSON APIs (no auth needed):
- Greenhouse: `https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true`
- Lever: `https://api.lever.co/v0/postings/{company_handle}?mode=json`

**`crawler_linkedin.py`** uses Playwright (`headless=False` for scraping, `headless=True` for description fetching) to load LinkedIn public job search pages. After title/company filtering, it visits each job URL individually to scrape descriptions for the experience filter. LinkedIn's DOM selectors (`div.base-card`, `h3`, `h4`, `.job-search-card__location`) may drift over time.

## Credentials

Stored in `.env` (gitignored):
```
LINKEDIN_EMAIL=...
LINKEDIN_PASSWORD=...
```

## Ranker (`rank/ranker.py`)

Merges `jobs.csv` + `linkedin_jobs.csv`, filters out already-applied URLs (from `apply/applied_log.csv`), dedupes by `company + title` (keeping Canada locations), drops ineligible locations, and scores each job 0–100:

| Signal | Max pts | Logic |
|---|---|---|
| Title match | 35 | ML/AI engineer > data scientist > SWE |
| Company tier | 25 | Tier 1 (Google/OpenAI/etc) > Tier 2 (Stripe/Shopify/etc) > other |
| Location | 20 | Canada city/province > remote > US-only |
| Recency | 15 | <6h > <12h > <24h > older |
| Description keywords | 10 | LLM, RAG, PyTorch, agents, transformers, etc. |

Outputs `ranked_jobs.csv` with top 40. Applicator reads this file in rank order.

## Auto-applicator (`apply/applicator.py`)

Reads `ranked_jobs.csv` (run ranker first), detects ATS type, and submits applications via Playwright.
Skips any URL already in `apply/applied_log.csv`.

ATS targets: Greenhouse, Lever, LinkedIn Easy Apply, generic fallback.


## Workflow Orchestration

### 1. Plan Node Default
- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- If something goes sideways, STOP and re-plan immediately – don't keep pushing
- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity

### 2. Subagent Strategy
- Use subagents liberally to keep main context window clean
- Offload research, exploration, and parallel analysis to subagents
- For complex problems, throw more compute at it via subagents
- One task per subagent for focused execution

### 3. Self-Improvement Loop
- After ANY correction from the user: update `tasks/lessons.md` with the pattern
- Write rules for yourself that prevent the same mistake
- Ruthlessly iterate on these lessons until mistake rate drops
- Review lessons at session start for relevant project

### 4. Verification Before Done
- Never mark a task complete without proving it works
- Diff behavior between main and your changes when relevant
- Ask yourself: "Would a staff engineer approve this?"
- Run tests, check logs, demonstrate correctness

### 5. Demand Elegance (Balanced)
- For non-trivial changes: pause and ask "is there a more elegant way?"
- If a fix feels hacky: "Knowing everything I know now, implement the elegant solution"
- Skip this for simple, obvious fixes – don't over-engineer
- Challenge your own work before presenting it

### 6. Autonomous Bug Fixing
- When given a bug report: just fix it. Don't ask for hand-holding
- Point at logs, errors, failing tests – then resolve them
- Zero context switching required from the user
- Go fix failing CI tests without being told how

## Task Management

1. **Plan First**: Write plan to `tasks/todo.md` with checkable items  
2. **Verify Plan**: Check in before starting implementation  
3. **Track Progress**: Mark items complete as you go  
4. **Explain Changes**: High-level summary at each step  
5. **Document Results**: Add review section to `tasks/todo.md`  
6. **Capture Lessons**: Update `tasks/lessons.md` after corrections  

## Core Principles

- **Simplicity First**: Make every change as simple as possible. Impact minimal code  
- **No Laziness**: Find root causes. No temporary fixes. Senior developer standards  
- **Minimal Impact**: Changes should only touch what's necessary. Avoid broad changes  