import hashlib
import os
import re
import sys
import requests
import pandas as pd
import yaml
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent))
_USE_SHEETS = bool(os.getenv("SPREADSHEET_ID"))
if _USE_SHEETS:
    from utils.gsheets import write_sheet


def _make_job_key(title: str, company: str, location: str) -> str:
    def _n(s): return re.sub(r'[^a-z0-9]', '', (s or '').lower().strip())
    raw = f"{_n(title)}|{_n(company)}|{_n(location)}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def strip_html(text: str) -> str:
    if not text:
        return ""
    import html as _html
    text = _html.unescape(text)
    text = re.sub(r'<[^>]+>', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; job-crawler/1.0)"
}

COMPANY_BLOCKLIST = {
    "mercor",
    "dataannotation",
    "data annotation",
    "outlier",
    "appen",
    "remotasks",
    "lionbridge",
    "telus international",
    "taskus",
    "clickworker",
    "alignerr",
}

LOCATION_KEYWORDS = [
    "canada",
    "remote",  # include remote since they're often open to CA/US
    # Canadian cities/provinces
    "toronto",
    "vancouver",
    "montreal",
    "ottawa",
    "calgary",
    "ontario",
    "british columbia",
    "alberta",
    "quebec",
    # USA
    "united states",
    "usa",
    "u.s.",
    "new york",
    "san francisco",
    "seattle",
    "boston",
    "chicago",
    "los angeles",
    "austin",
    "denver",
    "atlanta",
    "california",
    "washington",
    "new york",
    "texas",
    "massachusetts",
    "illinois",
]


def is_blocked_company(company: str) -> bool:
    if not company:
        return False
    return any(blocked in company.lower() for blocked in COMPANY_BLOCKLIST)


def is_within_24h(posted_at) -> bool:
    if not posted_at:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    try:
        # Lever uses Unix ms timestamps
        if isinstance(posted_at, (int, float)):
            dt = datetime.fromtimestamp(posted_at / 1000, tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(posted_at).replace("Z", "+00:00"))
        return dt >= cutoff
    except Exception:
        return False


def is_in_region(location: str) -> bool:
    if not location:
        return False
    lower = location.lower()
    return any(kw in lower for kw in LOCATION_KEYWORDS)


TITLE_KEYWORDS = [
    "machine learning",
    "ml engineer",
    "data scientist",
    "data science",
    "ai engineer",
    "artificial intelligence",
    "applied scientist",
    "applied ml",
    "software engineer",
    "software developer",
    "sde",
    "swe",
    "forward deployed",
    "nlp",
    "computer vision",
    "deep learning",
    "research engineer",
    "research scientist",
    "platform engineer",
    "backend engineer",
    "full stack engineer",
    "fullstack engineer",
]


TITLE_EXCLUDE = [
    "senior",
    "sr.",
    " sr ",
    "sr ",
    "staff",
    "principal",
    "director",
    "manager",
    "vice president",
    " vp ",
    "head of",
    "lead ",
    " lead",
    "intern",
    "internship",
    "co-op",
    "coop",
    "distinguished",
    "fellow",
]

# Matches "6+ years", "7 years", "8-10 years", etc. in description
_YEARS_RE = re.compile(r"(\d+)\s*\+?\s*(?:to\s*\d+\s*)?years?", re.IGNORECASE)

PHD_PATTERNS = [
    "ph.d",
    "phd",
    "doctorate",
    "doctoral degree",
]


def is_entry_level(title: str, description: str) -> bool:
    t = (title or "").lower()
    d = (description or "").lower()

    # Exclude by title
    if any(kw in t for kw in TITLE_EXCLUDE):
        return False

    # Exclude if description requires a PhD
    if any(p in d for p in PHD_PATTERNS):
        return False

    # Exclude if description mentions more than 5 years of experience
    for match in _YEARS_RE.finditer(d):
        years = int(match.group(1))
        if years > 5:
            return False

    return True


def is_relevant(title: str) -> bool:
    if not title:
        return False
    lower = title.lower()
    return any(kw in lower for kw in TITLE_KEYWORDS)


def safe_get(url: str, params: Dict[str, Any] | None = None, timeout: int = 20):
    resp = requests.get(url, headers=HEADERS, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp


def fetch_greenhouse_jobs(board_token: str) -> List[Dict[str, Any]]:
    """
    Greenhouse Job Board API
    Example:
    https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true
    """
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs"
    data = safe_get(url, params={"content": "true"}).json()

    jobs = []
    for job in data.get("jobs", []):
        jobs.append({
            "company": board_token,
            "source": "greenhouse",
            "title": job.get("title"),
            "location": (job.get("location") or {}).get("name"),
            "team": (job.get("departments") or [{}])[0].get("name") if job.get("departments") else None,
            "employment_type": None,
            "url": job.get("absolute_url"),
            "description": strip_html(job.get("content")),
            "posted_at": job.get("updated_at"),
            "job_id": job.get("id"),
        })
    return jobs


def fetch_lever_jobs(company_handle: str) -> List[Dict[str, Any]]:
    """
    Lever postings API
    Common public endpoint:
    https://api.lever.co/v0/postings/{company_handle}?mode=json
    """
    url = f"https://api.lever.co/v0/postings/{company_handle}"
    data = safe_get(url, params={"mode": "json"}).json()

    jobs = []
    for job in data:
        categories = job.get("categories") or {}
        jobs.append({
            "company": company_handle,
            "source": "lever",
            "title": job.get("text"),
            "location": categories.get("location"),
            "team": categories.get("team"),
            "employment_type": categories.get("commitment"),
            "url": job.get("hostedUrl") or job.get("applyUrl"),
            "description": strip_html(job.get("descriptionPlain") or job.get("description")),
            "posted_at": job.get("createdAt"),
            "job_id": job.get("id"),
        })
    return jobs


def load_config(path: str = "companies.yaml") -> Dict[str, List[str]]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def dedupe_jobs(df: pd.DataFrame) -> pd.DataFrame:
    # 优先按 URL 去重，没有 URL 再按 source + company + title + location
    if "url" in df.columns:
        df = df.drop_duplicates(subset=["url"], keep="first")
    df = df.drop_duplicates(subset=["source", "company", "title", "location"], keep="first")
    return df


def main():
    config = load_config()
    all_jobs: List[Dict[str, Any]] = []

    for board in config.get("greenhouse", []):
        try:
            jobs = fetch_greenhouse_jobs(board)
            print(f"[greenhouse] {board}: {len(jobs)} jobs")
            all_jobs.extend(jobs)
            time.sleep(1)
        except Exception as e:
            print(f"[greenhouse] {board} failed: {e}")

    for company in config.get("lever", []):
        try:
            jobs = fetch_lever_jobs(company)
            print(f"[lever] {company}: {len(jobs)} jobs")
            all_jobs.extend(jobs)
            time.sleep(1)
        except Exception as e:
            print(f"[lever] {company} failed: {e}")

    if not all_jobs:
        print("No jobs fetched.")
        return

    df = pd.DataFrame(all_jobs)
    df = dedupe_jobs(df)

    # 简单清洗
    text_cols = ["title", "location", "team", "employment_type"]
    for col in text_cols:
        if col in df.columns:
            df[col] = df[col].fillna("").astype(str).str.strip()

    before = len(df)
    df = df[~df["company"].apply(is_blocked_company)].reset_index(drop=True)
    print(f"Filtered to {len(df)} jobs after removing blocked companies (dropped {before - len(df)})")

    before = len(df)
    df = df[df["title"].apply(is_relevant)].reset_index(drop=True)
    print(f"Filtered to {len(df)} relevant jobs by title (dropped {before - len(df)})")

    before = len(df)
    df = df[df["location"].apply(is_in_region)].reset_index(drop=True)
    print(f"Filtered to {len(df)} jobs in Canada/USA (dropped {before - len(df)})")

    before = len(df)
    df = df[df["posted_at"].apply(is_within_24h)].reset_index(drop=True)
    print(f"Filtered to {len(df)} jobs posted in last 24h (dropped {before - len(df)})")

    before = len(df)
    df = df[df.apply(lambda r: is_entry_level(r["title"], r.get("description", "")), axis=1)].reset_index(drop=True)
    print(f"Filtered to {len(df)} entry-level jobs (dropped {before - len(df)})")

    df["job_key"] = df.apply(lambda r: _make_job_key(r["title"], r["company"], r["location"]), axis=1)

    if _USE_SHEETS:
        write_sheet("jobs", df)
        print(f"Saved {len(df)} jobs to Sheets:jobs")
    else:
        df.to_csv("jobs.csv", index=False, encoding="utf-8-sig")
        print(f"Saved {len(df)} jobs to jobs.csv")


if __name__ == "__main__":
    main()