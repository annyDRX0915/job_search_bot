"""
Gmail job alert crawler — parses LinkedIn and Indeed alert emails into job records.

Writes to:
  Sheets:email_jobs        — parsed job listings (fed into ranker.py)
  Sheets:important_emails  — interview/offer/assessment alerts (fed into notifier.py)

Usage:
    .venv/bin/python crawler/crawler_email.py
"""

import base64
import hashlib
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
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

IMPORTANT_KEYWORDS = [
    "interview", "invitation", "offer letter", "next steps", "assessment",
    "take-home", "technical screen", "phone screen", "background check",
    "congratulations", "rejected", "unfortunately", "we'd like to move forward",
]


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


# ── email body extraction ─────────────────────────────────────────────────────

def _get_body(payload: dict) -> str:
    """Recursively extract the HTML body from a Gmail message payload."""
    mime = payload.get("mimeType", "")
    if mime == "text/html":
        data = payload.get("body", {}).get("data", "")
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace") if data else ""
    if mime.startswith("multipart/"):
        parts = payload.get("parts", [])
        # Prefer text/html part directly
        for part in parts:
            if part.get("mimeType") == "text/html":
                return _get_body(part)
        # Recurse into nested multipart
        for part in parts:
            result = _get_body(part)
            if result:
                return result
    return ""

def _header(headers: list, name: str) -> str:
    return next((h["value"] for h in headers if h["name"].lower() == name.lower()), "")


# ── Gmail fetch ───────────────────────────────────────────────────────────────

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


# ── parsers ───────────────────────────────────────────────────────────────────

_JUNK_SUFFIXES = re.compile(
    r'\s*(actively recruiting|easy apply|\d+\s*school alum(?:ni)?|be an early applicant)\s*',
    re.IGNORECASE,
)


def parse_linkedin_jobs(html: str) -> list[dict]:
    """Parse LinkedIn job alert emails.

    Each job card has two <a> tags sharing the same /comm/jobs/view/{id} URL:
      - Short link: just the job title
      - Long link:  title + company + ' · ' + location (all concatenated)
    """
    from collections import defaultdict
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
        # Title = shortest text without '·'
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
            location = _JUNK_SUFFIXES.sub("", right).strip().rstrip(",").strip()
            break

        jobs.append({
            "source": "email_linkedin",
            "title": title,
            "company": company,
            "location": location,
            "url": f"https://www.linkedin.com/jobs/view/{job_id}/",
            "posted_at": datetime.now(timezone.utc).isoformat(),
            "description": "",
            "job_key": _make_job_key(title, company, location),
        })

    return jobs


def parse_indeed_jobs(html: str) -> list[dict]:
    """Parse Indeed job alert emails."""
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
        if not title or len(title) < 4 or "·" in title:
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
            "posted_at": datetime.now(timezone.utc).isoformat(),
            "description": "",
            "job_key": _make_job_key(title, company, location),
        })

    return jobs


# ── importance check ──────────────────────────────────────────────────────────

def is_important(subject: str) -> bool:
    s = subject.lower()
    return any(kw in s for kw in IMPORTANT_KEYWORDS)


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    service = _gmail_service()
    print("Gmail auth OK")

    # ── Job alert emails ──────────────────────────────────────────────────────
    job_alert_query = (
        "from:(jobalerts-noreply@linkedin.com OR alert@indeed.com) "
        "newer_than:1d"
    )
    print("Fetching job alert emails...")
    alert_msgs = fetch_messages(service, job_alert_query)
    print(f"  Found {len(alert_msgs)} email(s)")

    all_jobs = []
    for msg in alert_msgs:
        payload = msg["payload"]
        headers = payload.get("headers", [])
        subject = _header(headers, "subject")
        sender  = _header(headers, "from")
        body    = _get_body(payload)
        if not body:
            continue

        if "linkedin.com" in sender.lower():
            jobs = parse_linkedin_jobs(body)
        else:
            jobs = parse_indeed_jobs(body)

        print(f"  {sender[:45]!r}  '{subject[:50]}' → {len(jobs)} jobs")
        all_jobs.extend(jobs)

    # ── Important emails (interview / offer / assessment) ─────────────────────
    important_query = (
        "newer_than:1d "
        "-from:(jobalerts-noreply@linkedin.com OR alert@indeed.com "
        "OR noreply@ OR no-reply@ OR notifications@)"
    )
    print("\nChecking for important emails...")
    important_msgs = fetch_messages(service, important_query, max_results=30)

    important = []
    for msg in important_msgs:
        headers = msg["payload"].get("headers", [])
        subject = _header(headers, "subject")
        if not is_important(subject):
            continue
        body = _get_body(msg["payload"])
        important.append({
            "subject": subject,
            "sender":  _header(headers, "from"),
            "date":    _header(headers, "date"),
            "body":    body[:3000],
        })
        print(f"  [IMPORTANT] {subject}")

    # ── Save ──────────────────────────────────────────────────────────────────
    if all_jobs:
        df = pd.DataFrame(all_jobs).drop_duplicates(subset="url").reset_index(drop=True)
        print(f"\n{len(df)} unique jobs from email alerts")
        if _USE_SHEETS:
            write_sheet("email_jobs", df)
            print("Saved to Sheets:email_jobs")
        else:
            df.to_csv("email_jobs.csv", index=False)
            print("Saved to email_jobs.csv")
    else:
        print("\nNo jobs found in email alerts.")

    if important:
        imp_df = pd.DataFrame(important)
        if _USE_SHEETS:
            write_sheet("important_emails", imp_df)
            print(f"{len(important)} important email(s) saved to Sheets:important_emails")
        else:
            imp_df.to_csv("important_emails.csv", index=False)
            print(f"{len(important)} important email(s) saved to important_emails.csv")
    else:
        print("No important emails found.")


if __name__ == "__main__":
    main()
