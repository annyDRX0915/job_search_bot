"""
Job ranker — scores today's crawled jobs and outputs top 40 to ranked_jobs.csv.

Usage:
    .venv/bin/python rank/ranker.py
"""

import hashlib
import json
import os
import re
import sys
import pandas as pd
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent))
_USE_SHEETS = bool(os.getenv("SPREADSHEET_ID"))
if _USE_SHEETS:
    from utils.gsheets import read_sheet, write_sheet


def _make_job_key(title: str, company: str, location: str) -> str:
    def _n(s): return re.sub(r'[^a-z0-9]', '', (s or '').lower().strip())
    raw = f"{_n(title)}|{_n(company)}|{_n(location)}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]

# ── inputs / outputs ──────────────────────────────────────────────────────────
JOBS_CSV        = "jobs.csv"
LINKEDIN_CSV    = "linkedin_jobs.csv"
APPLIED_LOG     = "apply/applied_log.csv"
OUTPUT_CSV      = "ranked_jobs.csv"
TOP_N           = 50

# ── salary filter ─────────────────────────────────────────────────────────────
# Per-currency senior thresholds. Add a new entry here when adding a new country.
SENIOR_SALARY_THRESHOLD = {
    "CAD": 160_000,
    "USD": 200_000,
    "SGD": 130_000,
    "GBP":  80_000,
    "AUD": 150_000,
}

# Location keywords → currency fallback (used when description has no explicit currency)
_LOCATION_CURRENCY = [
    (["singapore"],                                                        "SGD"),
    (["canada", "toronto", "vancouver", "montreal", "ottawa", "calgary",
      "ontario", "british columbia", "alberta", "quebec", "waterloo"],    "CAD"),
    (["united kingdom", " uk ", "london"],                                 "GBP"),
    (["australia", "sydney", "melbourne"],                                 "AUD"),
]  # anything else → USD

# Matches: $150,000 / $150k / CAD $180k / S$130,000 and optional upper-bound range.
# Only fires on $ or S$ — other currency symbols (¥, ₹, €) are intentionally ignored
# since those jobs are already dropped by the ineligible-location filter.
_SALARY_RE = re.compile(
    r'(?:(CAD|USD|SGD|GBP|AUD|S)\s*)?\$\s*(\d{1,3}(?:,\d{3})+|\d{1,3}[kK])'
    r'(?:\s*[-–—to]+\s*(?:(?:CAD|USD|SGD|GBP|AUD|S)\s*)?\$?\s*'
    r'(\d{1,3}(?:,\d{3})+|\d{1,3}[kK]))?',
    re.IGNORECASE,
)

def _parse_amount(s: str) -> float:
    s = s.replace(",", "").strip()
    return float(s[:-1]) * 1000 if s[-1].lower() == "k" else float(s)

def _infer_currency(description: str, location: str) -> str:
    snippet = description[:3000] if description else ""
    if re.search(r'\bS\$|\bSGD\b', snippet, re.IGNORECASE):
        return "SGD"
    for code in ("CAD", "USD", "GBP", "AUD"):
        if re.search(rf'\b{code}\b', snippet, re.IGNORECASE):
            return code
    loc = (location or "").lower()
    for terms, code in _LOCATION_CURRENCY:
        if any(t in loc for t in terms):
            return code
    return "USD"

def _extract_salary_range(description: str, location: str) -> str:
    """Return a human-readable salary string like '$120k–$150k CAD', or '' if not found."""
    if not description:
        return ""
    currency = _infer_currency(description, location)
    for m in _SALARY_RE.finditer(description[:3000]):
        lower = _parse_amount(m.group(2))
        if lower < 40_000:
            continue
        def fmt(n: float) -> str:
            return f"${int(n // 1000)}k" if n % 1000 == 0 else f"${int(n):,}"
        upper = _parse_amount(m.group(3)) if m.group(3) else None
        result = fmt(lower)
        if upper:
            result += f"–{fmt(upper)}"
        return f"{result} {currency}"
    return ""

def _salary_too_high(description: str, location: str) -> bool:
    """Return True if the description clearly states a base salary above the senior threshold."""
    if not description:
        return False
    currency = _infer_currency(description, location)
    threshold = SENIOR_SALARY_THRESHOLD.get(currency, 200_000)
    for m in _SALARY_RE.finditer(description[:3000]):
        currency_prefix = (m.group(1) or "").upper()
        if currency_prefix == "S":   # S$ → SGD
            effective_threshold = SENIOR_SALARY_THRESHOLD["SGD"]
        else:
            effective_threshold = threshold
        lower = _parse_amount(m.group(2))
        if lower < 40_000:           # skip hourly/monthly figures
            continue
        if lower > effective_threshold:
            return True
    return False

# ── ranker-level blocklist (spam recruiters, gig platforms) ───────────────────
COMPANY_BLOCKLIST = {
    "great value hiring",
    "turing",           # gig-style "remote engineer" platform
    "alignerr",
}

# ── locations that indicate ineligible work jurisdiction ─────────────────────
INELIGIBLE_LOCATION_TERMS = [
    "india", "uk", "united kingdom", "london", "bangalore", "hyderabad",
    "mumbai", "delhi", "berlin", "germany", "australia", "sydney",
    "singapore", "ireland", "dublin", "france", "paris", "netherlands",
    "amsterdam", "poland", "warsaw",
]

# ── scoring weights ───────────────────────────────────────────────────────────
W_TITLE     = 30
W_COMPANY   = 25
W_LOCATION  = 20
W_RECENCY   = 15
W_KEYWORDS  = 10

# ── title scoring ─────────────────────────────────────────────────────────────
TITLE_SCORES = [
    (35, [r"\bml engineer\b", r"machine learning engineer", r"ai engineer"]),
    (30, [r"applied (scientist|ml|ai)", r"research engineer", r"mlops"]),
    (25, [r"data scientist", r"data science", r"nlp engineer", r"computer vision"]),
    (20, [r"data engineer", r"analytics engineer"]),
    (5, [r"software engineer", r"backend engineer", r"swe\b", r"full[- ]?stack"]),
    (0, [r"software developer", r"frontend engineer", r"platform engineer"]),
]

def score_title(title: str) -> int:
    t = title.lower()
    for pts, patterns in TITLE_SCORES:
        if any(re.search(p, t) for p in patterns):
            return pts
    return 5  # generic match from crawler filter

# ── company tier scoring ──────────────────────────────────────────────────────
TIER1 = {
    "google", "deepmind", "google deepmind", "meta", "apple", "amazon", "aws",
    "microsoft", "netflix", "openai", "anthropic", "nvidia", "databricks",
    "hugging face", "cohere", "stability ai", "scale ai",
}
TIER2 = {
    "stripe", "shopify", "uber", "airbnb", "lyft", "twitter", "x corp",
    "linkedin", "salesforce", "twilio", "datadog", "snowflake", "palantir",
    "confluent", "elastic", "mongodb", "cloudflare", "figma", "notion",
    "asana", "zendesk", "okta", "pagerduty", "hashicorp", "wealthsimple",
    "rbc", "td bank", "bmo", "scotiabank", "cibc", "desjardins",
    "rogers", "telus", "bell", "cgi", "sap", "servicenow",
    "intuit", "autodesk", "hootsuite", "d2l", "wish",
}

def score_company(company: str) -> int:
    c = company.lower().strip()
    if c in TIER1:
        return 25
    if c in TIER2:
        return 18
    return 10  # unknown — still worth applying

# ── location scoring ──────────────────────────────────────────────────────────
CANADA_TERMS = [
    "canada", "toronto", "vancouver", "montreal", "ottawa", "calgary",
    "ontario", "british columbia", "alberta", "quebec", "waterloo",
]

def score_location(location: str) -> int:
    if not location:
        return 3
    loc = location.lower()
    if any(t in loc for t in CANADA_TERMS):
        return 30
    if "remote" in loc:
        return 5   # remote with no Canada mention — likely US-based
    return 0  # US-only

# ── recency scoring ───────────────────────────────────────────────────────────
def score_recency(posted_at) -> int:
    if not posted_at or pd.isna(posted_at):
        return 5
    now = datetime.now(timezone.utc)
    try:
        if isinstance(posted_at, str):
            dt = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
        else:
            dt = pd.Timestamp(posted_at).to_pydatetime()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = now - dt
        if age <= timedelta(hours=6):
            return 15
        if age <= timedelta(hours=12):
            return 12
        if age <= timedelta(hours=24):
            return 8
        return 3
    except Exception:
        return 5

# ── description keyword scoring ───────────────────────────────────────────────
KEYWORDS = [
    "llm", "large language model", "rag", "retrieval", "agent",
    "transformer", "pytorch", "fine-tun", "embedding", "vector",
    "diffusion", "generative", "reinforcement learning",
]

def score_keywords(desc: str) -> int:
    if not desc:
        return 0
    d = desc.lower()
    hits = sum(1 for kw in KEYWORDS if kw in d)
    return min(hits * 2, W_KEYWORDS)

# ── AI re-ranker ──────────────────────────────────────────────────────────────

def _load_applied_history(n: int = 80) -> list[dict]:
    """Return the last n applied/skipped entries for AI context."""
    try:
        log = pd.read_csv(APPLIED_LOG)
        cols = [c for c in ["company", "title", "location", "status"] if c in log.columns]
        return log[cols].tail(n).to_dict(orient="records")
    except FileNotFoundError:
        return []


def ai_rerank(jobs: pd.DataFrame) -> pd.DataFrame:
    """Re-rank jobs using OpenAI, informed by applied/skipped history. Falls back to rule score on failure."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY not set — skipping AI re-ranking")
        return jobs

    history = _load_applied_history()

    job_list = []
    for _, row in jobs.iterrows():
        job_list.append({
            "job_key": row["job_key"],
            "title": row["title"],
            "company": row["company"],
            "location": row.get("location", ""),
            "salary": row.get("salary", ""),
            "rule_score": int(row["score"]),
            "description": str(row.get("description", ""))[:200],
        })

    history_block = json.dumps(history, indent=2) if history else "No history yet."
    jobs_block = json.dumps(job_list, indent=2)

    prompt = (
        "You are ranking job opportunities for a new grad (0–2 years experience) "
        "seeking ML/AI/data science or SWE roles in Canada (preferred) or remote.\n\n"
        f"Recent application history (applied vs skipped — learn from patterns):\n{history_block}\n\n"
        "Rank the jobs below. Prefer:\n"
        "- ML/AI/data science > generic SWE\n"
        "- Canada locations > remote > US-only\n"
        "- Strong companies and specific roles over vague catch-all postings\n"
        "- Patterns from the history above (if certain companies/titles were always skipped, score them lower)\n\n"
        f"Jobs:\n{jobs_block}\n\n"
        'Return JSON: {"rankings": [{"job_key": "...", "ai_score": 0-100, "ai_reason": "one line"}, ...]}'
    )

    try:
        from openai import OpenAI
        resp = OpenAI(api_key=api_key).chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
            max_tokens=6000,
        )
        result = json.loads(resp.choices[0].message.content)
        rankings = result.get("rankings", [])
        score_map = {r["job_key"]: (r["ai_score"], r["ai_reason"]) for r in rankings}

        jobs = jobs.copy()
        jobs["ai_score"] = jobs["job_key"].map(lambda k: score_map.get(k, (None, ""))[0])
        jobs["ai_reason"] = jobs["job_key"].map(lambda k: score_map.get(k, (None, ""))[1])
        jobs = (jobs.sort_values("ai_score", ascending=False)
                    .reset_index(drop=True))
        print("AI re-ranking complete.")
    except Exception as e:
        print(f"AI re-ranking failed ({e}) — keeping rule-based order")

    return jobs


# ── main ──────────────────────────────────────────────────────────────────────

def load_jobs() -> pd.DataFrame:
    frames = []
    keep = ["company", "title", "location", "url", "posted_at", "description", "job_key"]

    def _add(df: pd.DataFrame, label: str):
        df = df.rename(columns=lambda c: c.lstrip("\ufeff"))
        cols = [c for c in keep if c in df.columns]
        frames.append(df[cols])

    if _USE_SHEETS:
        for tab in ("jobs", "linkedin_jobs", "email_jobs"):
            df = read_sheet(tab)
            if not df.empty:
                _add(df, tab)
            else:
                print(f"Warning: Sheets:{tab} is empty, skipping")
    else:
        try:
            _add(pd.read_csv(JOBS_CSV), JOBS_CSV)
        except FileNotFoundError:
            print(f"Warning: {JOBS_CSV} not found, skipping")
        try:
            _add(pd.read_csv(LINKEDIN_CSV), LINKEDIN_CSV)
        except FileNotFoundError:
            print(f"Warning: {LINKEDIN_CSV} not found, skipping")

    if not frames:
        raise SystemExit("No job CSVs found.")

    df = pd.concat(frames, ignore_index=True).drop_duplicates(subset="url")

    # Compute job_key if crawlers haven't already
    if "job_key" not in df.columns:
        df["job_key"] = [_make_job_key(r["title"], r["company"], r["location"]) for _, r in df.iterrows()]

    # Dedupe same role at same company: keep Canada location first, then first seen
    canada_terms = ["canada", "toronto", "vancouver", "montreal", "ottawa",
                    "calgary", "ontario", "british columbia", "alberta", "quebec"]
    df["_canada"] = df["location"].fillna("").str.lower().apply(
        lambda l: any(t in l for t in canada_terms)
    )
    df = (df.sort_values("_canada", ascending=False)
            .drop_duplicates(subset="job_key")
            .drop(columns="_canada"))

    # Drop ineligible locations (countries user can't work in)
    def is_ineligible(loc: str) -> bool:
        if not loc:
            return False
        l = loc.lower()
        return any(t in l for t in INELIGIBLE_LOCATION_TERMS)

    before = len(df)
    df = df[~df["location"].fillna("").apply(is_ineligible)]
    print(f"Dropped {before - len(df)} jobs in ineligible locations")

    # Drop blocklisted companies
    before = len(df)
    df = df[~df["company"].fillna("").str.lower().isin(COMPANY_BLOCKLIST)]
    print(f"Dropped {before - len(df)} jobs from blocklisted companies")

    # Drop jobs with salaries clearly above senior thresholds
    before = len(df)
    df = df[~df.apply(lambda r: _salary_too_high(r.get("description", ""), r.get("location", "")), axis=1)]
    print(f"Dropped {before - len(df)} jobs with senior-level salaries")

    return df


def _norm_url(url: str) -> str:
    return str(url).strip().rstrip("/").lower().split("?")[0]

def load_applied() -> tuple[set, set, set]:
    """Return (applied_urls, applied_company_titles, applied_job_keys) from the log."""
    try:
        log = read_sheet("applied_log") if _USE_SHEETS else pd.read_csv(APPLIED_LOG)
        if log.empty:
            return set(), set(), set()
        urls = {_norm_url(u) for u in log["url"].dropna()}
        company_titles = set()
        if "company" in log.columns and "title" in log.columns:
            ct = log.dropna(subset=["company", "title"])
            company_titles = set(zip(ct["company"].str.strip().str.lower(), ct["title"].str.strip().str.lower()))
        job_keys = set(log["job_key"].dropna()) if "job_key" in log.columns else set()
        return urls, company_titles, job_keys
    except FileNotFoundError:
        return set(), set(), set()


def score_jobs(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["score_title"]    = df["title"].fillna("").apply(score_title)
    df["score_company"]  = df["company"].fillna("").apply(score_company)
    df["score_location"] = df["location"].fillna("").apply(score_location)
    df["score_recency"]  = df["posted_at"].apply(score_recency)
    df["score_keywords"] = df["description"].fillna("").apply(score_keywords)
    df["score"] = (
        df["score_title"] +
        df["score_company"] +
        df["score_location"] +
        df["score_recency"] +
        df["score_keywords"]
    )
    df["salary"] = df.apply(lambda r: _extract_salary_range(r.get("description", ""), r.get("location", "")), axis=1)
    return df


def main():
    jobs = load_jobs()
    print(f"Loaded {len(jobs)} total jobs")

    applied_urls, applied_ct, applied_keys = load_applied()

    def already_applied(row) -> bool:
        if _norm_url(row["url"]) in applied_urls:
            return True
        if row.get("job_key") and row["job_key"] in applied_keys:
            return True
        c = (row.get("company") or "").strip().lower()
        t = (row.get("title") or "").strip().lower()
        return bool(c and t and (c, t) in applied_ct)

    before = len(jobs)
    jobs = jobs[~jobs.apply(already_applied, axis=1)].reset_index(drop=True)
    print(f"After removing already-applied: {len(jobs)} remaining (dropped {before - len(jobs)})")

    jobs = score_jobs(jobs)
    jobs = jobs.sort_values("score", ascending=False).reset_index(drop=True)

    top = jobs.head(TOP_N)
    top = ai_rerank(top)
    top = top.reset_index(drop=True)
    top.insert(0, "rank", top.index + 1)

    # Save full ranked list (without description to keep it readable)
    base_cols = ["rank", "score", "company", "title", "location", "salary", "posted_at", "url",
                 "job_key", "score_title", "score_company", "score_location", "score_recency", "score_keywords"]
    ai_cols = [c for c in ["ai_score", "ai_reason"] if c in top.columns]
    out = top[base_cols + ai_cols]
    if _USE_SHEETS:
        write_sheet("ranked_jobs", out)
        print(f"Top {TOP_N} saved to Sheets:ranked_jobs")
    else:
        out.to_csv(OUTPUT_CSV, index=False)
        print(f"Top {TOP_N} saved to {OUTPUT_CSV}")

    display_cols = ["rank", "score"] + ai_cols[:1] + ["company", "title", "location", "salary"]
    print(top[[c for c in display_cols if c in top.columns]].to_string(index=False))


if __name__ == "__main__":
    main()
