"""
Gmail job alert crawler — parses LinkedIn, Indeed, and Glassdoor alert emails into job records.

Writes to Sheets:email_jobs (or email_jobs.csv as fallback).

Usage:
    .venv/bin/python crawler/crawler_email.py
"""

import base64
import concurrent.futures
import hashlib
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent.parent))
_USE_SHEETS = bool(os.getenv("SPREADSHEET_ID"))
if _USE_SHEETS:
    from utils.gsheets import write_sheet

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


# ── auth ──────────────────────────────────────────────────────────────────────

def _gmail_service():
    for var in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"):
        if not os.getenv(var):
            raise SystemExit(f"{var} not set in .env")
    creds = Credentials(
        token=None,
        refresh_token=os.getenv("GMAIL_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.getenv("GMAIL_CLIENT_ID"),
        client_secret=os.getenv("GMAIL_CLIENT_SECRET"),
        scopes=SCOPES,
    )
    return build("gmail", "v1", credentials=creds)


# ── email helpers ─────────────────────────────────────────────────────────────

def _get_body(payload: dict) -> str:
    mime = payload.get("mimeType", "")
    if mime == "text/html":
        data = payload.get("body", {}).get("data", "")
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace") if data else ""
    if mime.startswith("multipart/"):
        parts = payload.get("parts", [])
        for part in parts:
            if part.get("mimeType") == "text/html":
                return _get_body(part)
        for part in parts:
            result = _get_body(part)
            if result:
                return result
    return ""

def _header(headers: list, name: str) -> str:
    return next((h["value"] for h in headers if h["name"].lower() == name.lower()), "")

def fetch_messages(service, query: str, max_results: int = 50) -> list:
    res = service.users().messages().list(
        userId="me", q=query, maxResults=max_results
    ).execute()
    messages = []
    for msg in res.get("messages", []):
        full = service.users().messages().get(
            userId="me", id=msg["id"], format="full"
        ).execute()
        messages.append(full)
    return messages


# ── job key ───────────────────────────────────────────────────────────────────

def _make_job_key(title: str, company: str, location: str) -> str:
    def _n(s): return re.sub(r'[^a-z0-9]', '', (s or '').lower().strip())
    return hashlib.md5(f"{_n(title)}|{_n(company)}|{_n(location)}".encode()).hexdigest()[:12]


# ── description fetcher ──────────────────────────────────────────────────────

import time as _time


def _recency_tag_to_dt(tag: str, base: datetime) -> datetime:
    """Convert '6h', '1d', 'Just posted', 'Today' relative to base datetime."""
    tag = tag.strip().lower()
    if tag in ("just posted", "today"):
        return base
    m = re.match(r'(\d+)([dh])', tag)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return base - (timedelta(days=n) if unit == 'd' else timedelta(hours=n))
    return base


def _is_recent(posted_at: str, hours: int = 48) -> bool:
    try:
        dt = datetime.fromisoformat(posted_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() < hours * 3600
    except Exception:
        return True


# ── job filters (mirrors crawler_linkedin.py) ─────────────────────────────────

_COMPANY_BLOCKLIST = {
    "mercor", "dataannotation", "data annotation", "outlier", "appen",
    "scale ai", "remotasks", "lionbridge", "telus international",
    "taskus", "clickworker", "alignerr", "jobs ai",
}

_TITLE_KEYWORDS = [
    "machine learning", "ml engineer", "data scientist", "data science",
    "ai engineer", "artificial intelligence", "applied scientist", "applied ml",
    "software engineer", "software developer", "sde", "swe",
    "forward deployed", "nlp", "computer vision", "deep learning",
    "research engineer", "research scientist", "platform engineer",
    "backend engineer", "full stack engineer", "fullstack engineer",
    "gen ai", "generative ai", "llm",
]

_TITLE_EXCLUDE = [
    "senior", "sr.", " sr ", "sr ", "staff", "principal", "director",
    "manager", "vice president", " vp ", "head of", "lead ", " lead",
    "student", "co-op", "coop", "distinguished", "fellow",
]

_YEARS_RE = re.compile(r"(\d+)\s*\+?\s*(?:to\s*\d+\s*)?years?", re.IGNORECASE)
_PHD_PATTERNS = ["ph.d", "phd", "doctorate", "doctoral degree"]
_SENIOR_PHRASES = [
    "5+ years", "6+ years", "7+ years", "8+ years",
    "senior engineer", "senior developer", "senior scientist",
    "lead engineer", "staff engineer", "principal engineer",
]


def _is_relevant(title: str) -> bool:
    lower = (title or "").lower()
    return any(kw in lower for kw in _TITLE_KEYWORDS)


def _is_entry_level(title: str) -> bool:
    lower = (title or "").lower()
    return not any(kw in lower for kw in _TITLE_EXCLUDE)


def _passes_description_filter(description: str) -> bool:
    if not description:
        return True  # no description — don't reject, ranker will score low
    d = description.lower()
    if any(p in d for p in _PHD_PATTERNS):
        return False
    if any(p in d for p in _SENIOR_PHRASES):
        return False
    for m in _YEARS_RE.finditer(d):
        if int(m.group(1)) >= 5:
            return False
    return True


def _is_blocked_company(company: str) -> bool:
    lower = (company or "").lower()
    return any(blocked in lower for blocked in _COMPANY_BLOCKLIST)


def _apply_filters(jobs: list[dict]) -> list[dict]:
    before = len(jobs)
    kept = []
    for j in jobs:
        if not _is_recent(j.get("posted_at", ""), hours=120):
            continue
        if not _is_relevant(j["title"]):
            continue
        if not _is_entry_level(j["title"]):
            continue
        if _is_blocked_company(j["company"]):
            continue
        if not _passes_description_filter(j.get("description", "")):
            continue
        kept.append(j)
    print(f"  Filtered {before - len(kept)} jobs → {len(kept)} remain")
    return kept


_REQ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-CA,en-US;q=0.9,en;q=0.8",
}
_REQ_SESSION = requests.Session()
_REQ_SESSION.headers.update(_REQ_HEADERS)


def _fetch_linkedin_description(url: str) -> tuple[str, str]:
    """Fetch LinkedIn public job page via requests with retry on 429.

    Returns (description, posted_at_iso). posted_at_iso is "" when unavailable.
    """
    for attempt in range(3):
        try:
            resp = _REQ_SESSION.get(url, timeout=12, allow_redirects=True)
            if resp.status_code == 429:
                _time.sleep(3 * (attempt + 1))
                continue
            if resp.status_code != 200:
                return "", ""
            soup = BeautifulSoup(resp.text, "html.parser")
            el = (
                soup.find("div", class_=re.compile(r"show-more-less-html__markup", re.I))
                or soup.find("div", class_=re.compile(r"description__text", re.I))
                or soup.find("div", id="job-details")
                or soup.find("section", class_=re.compile(r"description", re.I))
            )
            description = el.get_text(separator="\n", strip=True)[:2500] if el else ""

            # Extract posting time: prefer <time datetime="..."> then relative text
            posted_at_iso = ""
            time_el = soup.find("time", attrs={"datetime": True})
            if time_el:
                try:
                    posted_at_iso = datetime.fromisoformat(
                        time_el["datetime"].replace("Z", "+00:00")
                    ).isoformat()
                except Exception:
                    pass
            if not posted_at_iso:
                rel_el = soup.find(class_=re.compile(r"posted-time-ago", re.I))
                if rel_el:
                    rel_text = rel_el.get_text(strip=True)
                    m = re.search(r'(\d+)\s+(hour|day|week|month)s?\s+ago', rel_text, re.I)
                    if m:
                        n, unit = int(m.group(1)), m.group(2).lower()
                        delta = {
                            "hour": timedelta(hours=n),
                            "day": timedelta(days=n),
                            "week": timedelta(weeks=n),
                            "month": timedelta(days=n * 30),
                        }.get(unit, timedelta(0))
                        posted_at_iso = (datetime.now(timezone.utc) - delta).isoformat()
                    elif re.search(r'just now|moments? ago|today', rel_text, re.I):
                        posted_at_iso = datetime.now(timezone.utc).isoformat()

            return description, posted_at_iso
        except Exception:
            break
    return "", ""



def _fetch_indeed_description_requests(url: str) -> str:
    """Fetch Indeed job description via requests; follows cts.indeed.com redirects."""
    try:
        resp = _REQ_SESSION.get(url, timeout=12, allow_redirects=True)
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.text, "html.parser")
        el = soup.find(id="jobDescriptionText")
        if el:
            return el.get_text(separator="\n", strip=True)[:2500]
    except Exception:
        pass
    return ""


def _fetch_indeed_descriptions_pw(jobs: list[dict]) -> None:
    """Fetch Indeed job descriptions — requests first, Playwright fallback.

    requests works for canonical viewjob?jk= URLs and follows cts.indeed.com
    redirects. Playwright (headless=False) covers any that requests can't fetch
    due to JS-gated content or security checks.
    """
    from playwright.sync_api import sync_playwright

    indeed_jobs = [j for j in jobs
                   if j["source"] in ("email_indeed_alert", "email_indeed_single")
                   and not j.get("description")]
    if not indeed_jobs:
        return

    # Pass 1: requests (fast, no bot detection)
    print(f"  Fetching {len(indeed_jobs)} Indeed descriptions via requests...")
    for job in indeed_jobs:
        desc = _fetch_indeed_description_requests(job["url"])
        if desc:
            job["description"] = desc
        _time.sleep(0.3)

    still_missing = [j for j in indeed_jobs if not j.get("description")]
    if not still_missing:
        return

    # Pass 2: Playwright headless=False for JS-rendered or blocked pages
    print(f"  Fetching {len(still_missing)} Indeed descriptions via Playwright...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        for job in still_missing:
            ctx = browser.new_context(user_agent=_REQ_HEADERS["User-Agent"])
            page = ctx.new_page()
            try:
                page.goto(job["url"], wait_until="networkidle", timeout=25000)
                el = page.query_selector("#jobDescriptionText")
                if el:
                    job["description"] = el.inner_text()[:2500]
            except Exception:
                pass
            finally:
                ctx.close()
            _time.sleep(0.5)
        browser.close()


def _enrich_descriptions(jobs: list[dict]) -> list[dict]:
    """Fetch descriptions for all parseable sources; mutates jobs in-place."""
    li_jobs = [j for j in jobs if j["source"] == "email_linkedin" and not j.get("description")]
    indeed_jobs = [j for j in jobs if j["source"] in ("email_indeed_alert", "email_indeed_single")]

    # LinkedIn: requests-based, throttled to avoid 429
    if li_jobs:
        print(f"  Fetching {len(li_jobs)} LinkedIn descriptions...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futs = {pool.submit(_fetch_linkedin_description, j["url"]): j for j in li_jobs}
            for fut in concurrent.futures.as_completed(futs):
                description, posted_at_iso = fut.result()
                job = futs[fut]
                job["description"] = description
                if posted_at_iso:
                    job["posted_at"] = posted_at_iso

    # Indeed: Playwright-based via jk redirect
    if indeed_jobs:
        _fetch_indeed_descriptions_pw(jobs)

    n = sum(1 for j in jobs if j.get("description"))
    print(f"  Got descriptions for {n}/{len(jobs)} jobs")
    return jobs


# ── shared junk patterns ──────────────────────────────────────────────────────

_JUNK_SUFFIXES = re.compile(
    r'\s*(actively recruiting|easy apply|\d+\s*school alum(?:ni)?|be an early applicant)\s*',
    re.IGNORECASE,
)
# Salary: "$80K - $95K ( Employer Est. )" or "$22/hr" etc.
_SALARY = re.compile(
    r'\s*\$[\d,]+[Kk]?(?:[/\w]*)(?:\s*[-–]\s*\$[\d,]+[Kk]?(?:[/\w]*))?(?:\s*\([^)]*[Ee]st\.?[^)]*\))?',
    re.IGNORECASE,
)
# Trailing numeric rating like "4.3", "3.x"
_RATING_SUFFIX = re.compile(r'\s*\d[\d.]*[a-z]?\s*$')
# Recency tags: "1d", "6h", "Just posted", "Today"
_RECENCY = re.compile(r'\s*(?:Just posted|Today|\d+[dh])\s*$', re.IGNORECASE)
# Parenthetical expressions "(CyberArk, SAAS)"
_PARENS = re.compile(r'\([^)]*\)')
# Generic button-like / footer link texts to skip
_BUTTON_TEXT = re.compile(
    r'^(apply|view|see|click|learn|find|browse|search|manage|update|login|sign in|unsubscribe|'
    r'get started|jobs near|more jobs|all jobs|set up|create|edit|terms|privacy|help|pause|'
    r'since |for last|this is a bad|report this|not interested|save job|dismiss|'
    r'cookie|contact us|accessibility|sitemap|copyright|indeed)\b',
    re.IGNORECASE,
)
# Pay frequency text that leaks into location
_PAY_FREQ = re.compile(
    r'\s+(?:a year|an hour|a week|a month|per hour|per year|hourly|annually)\b.*$',
    re.IGNORECASE,
)


# ── parsers ───────────────────────────────────────────────────────────────────

def parse_linkedin_jobs(html: str, email_dt: datetime | None = None) -> list[dict]:
    """Each job card has two <a> tags with the same /comm/jobs/view/{id} URL:
      - Short link: just the job title
      - Long link:  title + company + ' · ' + location (all concatenated)
    """
    soup = BeautifulSoup(html, "html.parser")
    by_id: dict[str, list[str]] = defaultdict(list)

    for a in soup.find_all("a", href=True):
        if "/comm/jobs/view/" not in a["href"]:
            continue
        job_id = a["href"].split("/comm/jobs/view/")[1].split("?")[0]
        text = a.get_text(separator=" ", strip=True)
        if text:
            by_id[job_id].append(text)

    jobs = []
    for job_id, texts in by_id.items():
        title_candidates = [t for t in texts if "·" not in t]
        if not title_candidates:
            continue
        title = min(title_candidates, key=len).strip()
        if not title or len(title) < 4:
            continue

        company, location = "", ""
        for text in texts:
            if "·" not in text:
                continue
            left, _, right = text.partition("·")
            left = left.strip()
            company = left[len(title):].strip() if left.startswith(title) else left.split()[-1]
            loc = _JUNK_SUFFIXES.sub("", right).strip().rstrip(",")
            loc = re.sub(r'\s*\(\d[^)]*\)\s*$', '', loc)  # "(123 applied)"
            loc = re.sub(r'\s+\d+\s*\w*\s*$', '', loc)    # " 3 comp", " 21 compa", " 1"
            loc = re.sub(r'\s*\(\s*$', '', loc)            # orphaned "("
            location = loc.strip()
            break

        jobs.append({
            "source": "email_linkedin",
            "title": title,
            "company": company,
            "location": location,
            "url": f"https://www.linkedin.com/jobs/view/{job_id.strip('/')}/",
            "posted_at": (email_dt or datetime.now(timezone.utc)).isoformat(),
            "description": "",
            "job_key": _make_job_key(title, company, location),
        })

    return jobs


_INDEED_SKIP = re.compile(
    r'(unsubscribe|editjobfilter|stopemails|optout|managesubscriptions|'
    r'privacy|terms|help|login|since_yesterday|for_last)',
    re.IGNORECASE,
)


def _indeed_job_url(href: str) -> str | None:
    """Return canonical URL for an Indeed job link, or None if not a job link."""
    if "indeed.com" not in href.lower():
        return None
    if _INDEED_SKIP.search(href):
        return None
    # Normalize: use jk= as dedup key when present
    m = re.search(r'[?&]jk=([^&]+)', href)
    if m:
        return f"https://www.indeed.com/viewjob?jk={m.group(1)}"
    return href.split("?")[0]


def parse_indeed_alert_jobs(html: str, email_dt: datetime | None = None) -> list[dict]:
    """Each job card has two <a> tags pointing to the same Indeed job URL.
    Handles engage.indeed.com tracking links, r.indeed.com redirects, and
    direct indeed.com/rc/clk?jk= links used in company-specific alert emails.
    """
    soup = BeautifulSoup(html, "html.parser")
    by_url: dict[str, list[str]] = defaultdict(list)

    for a in soup.find_all("a", href=True):
        url = _indeed_job_url(a["href"])
        if not url:
            continue
        text = a.get_text(separator=" ", strip=True)
        if text and not _BUTTON_TEXT.match(text):
            by_url[url].append(text)

    jobs = []
    for url, texts in by_url.items():
        title = min(texts, key=len).strip()
        if not title or len(title) < 8 or _BUTTON_TEXT.match(title):
            continue

        base = email_dt or datetime.now(timezone.utc)
        posted_at = base
        company, location = "", ""
        for text in sorted(texts, key=len, reverse=True):
            if len(text) <= len(title):
                continue
            rest = text[len(title):].strip() if text.startswith(title) else text
            rest = _SALARY.sub("", rest).strip()
            recency_m = _RECENCY.search(rest)
            if recency_m:
                posted_at = _recency_tag_to_dt(recency_m.group(), base)
            rest = _RECENCY.sub("", rest).strip()
            rest = _JUNK_SUFFIXES.sub("", rest).strip()
            if " - " in rest:
                company_part, _, loc = rest.partition(" - ")
                company = _RATING_SUFFIX.sub("", company_part).strip()
                loc = _JUNK_SUFFIXES.sub("", loc).strip().split("$")[0].strip()
                location = _PAY_FREQ.sub("", loc).strip()
            elif rest:
                company = _RATING_SUFFIX.sub("", rest).strip()
            break

        jobs.append({
            "source": "email_indeed_alert",
            "title": title,
            "company": company,
            "location": location,
            "url": url,
            "posted_at": posted_at.isoformat(),
            "description": "",
            "job_key": _make_job_key(title, company, location),
        })

    return jobs


def parse_indeed_single_job(html: str, email_dt: datetime | None = None) -> list[dict]:
    """Single job match email from donotreply@match.indeed.com.
    Job title is link text on cts.indeed.com tracking URLs.
    Company extracted from body text: 'this {title} role at {company}'.
    """
    soup = BeautifulSoup(html, "html.parser")
    body_text = soup.get_text(separator=" ", strip=True)
    jobs = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "cts.indeed.com" not in href:
            continue
        # Check button text BEFORE URL dedup: "View job" and the actual title
        # share the same tracking URL, so filtering first lets the title link through.
        title = a.get_text(separator=" ", strip=True)
        if not title or len(title) < 8 or _BUTTON_TEXT.match(title):
            continue
        url = href  # cts URLs have no query params to strip
        if url in seen:
            continue
        seen.add(url)

        company = ""
        m = re.search(
            r'this\s+' + re.escape(title) + r'\s+role\s+at\s+([^.,\n]+)',
            body_text, re.IGNORECASE,
        )
        if m:
            company = m.group(1).strip()

        jobs.append({
            "source": "email_indeed_single",
            "title": title,
            "company": company,
            "location": "",
            "url": url,
            "posted_at": (email_dt or datetime.now(timezone.utc)).isoformat(),
            "description": "",
            "job_key": _make_job_key(title, company, ""),
        })

    return jobs


def parse_glassdoor_jobs(html: str, email_dt: datetime | None = None) -> list[dict]:
    """Glassdoor job alert email from noreply@glassdoor.com.
    One link per job at glassdoor.*/partner/jobListing.htm.
    Text: '{Company} {X.X ★}? {Title} {Location} {$Salary}? {Easy Apply}? {recency}?'
    Company is only reliably parsed when ★ rating is present.
    """
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "glassdoor" not in href.lower() or "joblisting" not in href.lower():
            continue
        # Dedup by jobListingId; keep full href as URL (it's a click-tracking redirect
        # that resolves to the job page — stripping ? breaks it).
        m_id = re.search(r'[?&]jobListingId=(\d+)', href)
        if not m_id:
            continue
        dedup_key = m_id.group(1)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        url = href

        raw = a.get_text(separator=" ", strip=True)
        if not raw or len(raw) < 5:
            continue

        # Strip trailing junk right to left; capture recency tag for posted_at
        base = email_dt or datetime.now(timezone.utc)
        recency_m = _RECENCY.search(raw)
        posted_at = _recency_tag_to_dt(recency_m.group(), base) if recency_m else base
        text = _RECENCY.sub("", raw)
        text = re.sub(r'\s*Easy Apply\s*', '', text, flags=re.IGNORECASE).strip()
        text = _SALARY.sub("", text).strip()

        company, rest = "", text
        if "★" in text:
            left, _, right = text.partition("★")
            company = _RATING_SUFFIX.sub("", left).strip()
            rest = right.strip()

        # Separate title from location: strip parentheticals, then last word = location
        cleaned = _PARENS.sub("", rest).strip()
        words = cleaned.split()
        if len(words) > 1:
            location = words[-1]
            loc_idx = rest.rfind(words[-1])
            title = rest[:loc_idx].strip() if loc_idx > 0 else rest
        else:
            title = rest
            location = ""

        if not title or len(title) < 3:
            continue

        jobs.append({
            "source": "email_glassdoor",
            "title": title,
            "company": company,
            "location": location,
            "url": url,
            "posted_at": posted_at.isoformat(),
            "description": "",
            "job_key": _make_job_key(title, company, location),
        })

    return jobs


def parse_old_indeed_jobs(html: str, email_dt: datetime | None = None) -> list[dict]:
    """Fallback parser for legacy alert@indeed.com emails."""
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "indeed.com" not in href:
            continue
        url = href.split("?")[0]
        if url in seen:
            continue
        seen.add(url)

        title = a.get_text(separator=" ", strip=True)
        if not title or len(title) < 4 or "·" in title or _BUTTON_TEXT.match(title):
            continue

        company, location = "", ""
        container = a.find_parent(["td", "div"])
        if container:
            texts = [t.strip() for t in container.stripped_strings
                     if t.strip() and t.strip() != title]
            if texts and "·" in texts[0]:
                company, _, location = texts[0].partition("·")
                company  = company.strip()
                location = _JUNK_SUFFIXES.sub("", location).strip()
            else:
                company  = texts[0] if texts else ""
                location = texts[1] if len(texts) > 1 else ""

        jobs.append({
            "source": "email_indeed",
            "title": title,
            "company": company,
            "location": location,
            "url": url,
            "posted_at": (email_dt or datetime.now(timezone.utc)).isoformat(),
            "description": "",
            "job_key": _make_job_key(title, company, location),
        })

    return jobs


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    service = _gmail_service()
    print("Gmail auth OK")

    query = (
        "from:("
        "jobalerts-noreply@linkedin.com OR "
        "donotreply@jobalert.indeed.com OR "
        "donotreply@match.indeed.com OR "
        "noreply@glassdoor.com OR "
        "alert@indeed.com"
        ") newer_than:1d"
    )
    print("Fetching job alert emails...")
    messages = fetch_messages(service, query)
    print(f"  Found {len(messages)} email(s)")

    all_jobs = []
    for msg in messages:
        payload = msg["payload"]
        headers = payload.get("headers", [])
        subject = _header(headers, "subject")
        sender  = _header(headers, "from")
        body    = _get_body(payload)
        if not body:
            continue

        ts_ms = int(msg.get("internalDate", 0))
        email_dt = (
            datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc) if ts_ms
            else datetime.now(timezone.utc)
        )

        sender_lower = sender.lower()
        if "linkedin.com" in sender_lower:
            jobs = parse_linkedin_jobs(body, email_dt)
        elif "glassdoor.com" in sender_lower:
            jobs = parse_glassdoor_jobs(body, email_dt)
        elif "match.indeed.com" in sender_lower or "donotreply@match" in sender_lower:
            jobs = parse_indeed_single_job(body, email_dt)
        elif "jobalert.indeed.com" in sender_lower:
            jobs = parse_indeed_alert_jobs(body, email_dt)
        elif "indeed.com" in sender_lower:
            jobs = parse_old_indeed_jobs(body, email_dt)
        else:
            jobs = []

        print(f"  {sender[:45]!r}  '{subject[:50]}' → {len(jobs)} jobs")
        all_jobs.extend(jobs)

    if not all_jobs:
        print("No jobs found in email alerts.")
        return

    # Dedup by URL first, then fetch descriptions in parallel
    seen_urls: set[str] = set()
    unique_jobs = []
    for j in all_jobs:
        if j["url"] not in seen_urls:
            seen_urls.add(j["url"])
            unique_jobs.append(j)

    print(f"\n{len(unique_jobs)} unique jobs from email alerts")
    _enrich_descriptions(unique_jobs)
    unique_jobs = _apply_filters(unique_jobs)

    if not unique_jobs:
        print("No jobs passed filters.")
        return

    df = pd.DataFrame(unique_jobs).reset_index(drop=True)

    if _USE_SHEETS:
        write_sheet("email_jobs", df)
        print("Saved to Sheets:email_jobs")
    else:
        df.to_csv("email_jobs.csv", index=False)
        print("Saved to email_jobs.csv")


if __name__ == "__main__":
    main()
