"""
Job ranker — scores today's crawled jobs and outputs top 40 to ranked_jobs.csv.

Usage:
    .venv/bin/python rank/ranker.py
"""

import re
import pandas as pd
from datetime import datetime, timezone, timedelta

# ── inputs / outputs ──────────────────────────────────────────────────────────
JOBS_CSV        = "jobs.csv"
LINKEDIN_CSV    = "linkedin_jobs.csv"
APPLIED_LOG     = "apply/applied_log.csv"
OUTPUT_CSV      = "ranked_jobs.csv"
TOP_N           = 40

# ── ranker-level blocklist (spam recruiters, gig platforms) ───────────────────
COMPANY_BLOCKLIST = {
    "great value hiring",
    "turing",           # gig-style "remote engineer" platform
    "alignerr",
    "jobright",
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
    (15, [r"software engineer", r"backend engineer", r"swe\b", r"full[- ]?stack"]),
    (10, [r"software developer", r"frontend engineer", r"platform engineer"]),
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
        return 8
    loc = location.lower()
    if any(t in loc for t in CANADA_TERMS):
        return 20
    if "remote" in loc:
        return 15  # remote but no explicit Canada — might still be fine
    return 5  # US-only or unclear

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

# ── main ──────────────────────────────────────────────────────────────────────

def load_jobs() -> pd.DataFrame:
    frames = []

    try:
        df = pd.read_csv(JOBS_CSV)
        df = df.rename(columns=lambda c: c.lstrip("\ufeff"))
        frames.append(df[["company", "title", "location", "url", "posted_at", "description"]])
    except FileNotFoundError:
        print(f"Warning: {JOBS_CSV} not found, skipping")

    try:
        df = pd.read_csv(LINKEDIN_CSV)
        df = df.rename(columns=lambda c: c.lstrip("\ufeff"))
        frames.append(df[["company", "title", "location", "url", "posted_at", "description"]])
    except FileNotFoundError:
        print(f"Warning: {LINKEDIN_CSV} not found, skipping")

    if not frames:
        raise SystemExit("No job CSVs found.")

    df = pd.concat(frames, ignore_index=True).drop_duplicates(subset="url")

    # Dedupe same role at same company: keep Canada location first, then first seen
    canada_terms = ["canada", "toronto", "vancouver", "montreal", "ottawa",
                    "calgary", "ontario", "british columbia", "alberta", "quebec"]
    df["_canada"] = df["location"].fillna("").str.lower().apply(
        lambda l: any(t in l for t in canada_terms)
    )
    df = (df.sort_values("_canada", ascending=False)
            .drop_duplicates(subset=["company", "title"])
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

    return df


def load_applied_urls() -> set:
    try:
        log = pd.read_csv(APPLIED_LOG)
        return set(log["url"].dropna().tolist())
    except FileNotFoundError:
        return set()


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
    return df


def main():
    jobs = load_jobs()
    print(f"Loaded {len(jobs)} total jobs")

    applied = load_applied_urls()
    jobs = jobs[~jobs["url"].isin(applied)]
    print(f"After removing {len(applied)} already-applied: {len(jobs)} remaining")

    jobs = score_jobs(jobs)
    jobs = jobs.sort_values("score", ascending=False).reset_index(drop=True)
    jobs.insert(0, "rank", jobs.index + 1)

    top = jobs.head(TOP_N)

    # Save full ranked list (without description to keep it readable)
    cols = ["rank", "score", "company", "title", "location", "posted_at", "url",
            "score_title", "score_company", "score_location", "score_recency", "score_keywords"]
    top[cols].to_csv(OUTPUT_CSV, index=False)

    print(f"\nTop {TOP_N} saved to {OUTPUT_CSV}\n")
    print(top[["rank", "score", "company", "title", "location"]].to_string(index=False))


if __name__ == "__main__":
    main()
