"""
Persistent email memory backed by Google Sheets tab 'email_memory'.
Provides de-duplication, RAG context, and calendar event tracking.
"""
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_USE_SHEETS = bool(os.getenv("SPREADSHEET_ID"))
if _USE_SHEETS:
    from utils.gsheets import read_sheet, append_rows

SCHEMA = [
    "id", "processed_at", "category", "company", "title",
    "summary", "action", "calendar_event_id", "event_datetime",
]

_cache: dict | None = None  # loaded once per run: {email_id: record}


def _load() -> dict[str, dict]:
    global _cache
    if _cache is not None:
        return _cache
    if not _USE_SHEETS:
        _cache = {}
        return _cache
    try:
        df = read_sheet("email_memory")
        if df.empty or "id" not in df.columns:
            _cache = {}
        else:
            _cache = {
                str(row["id"]): row.to_dict()
                for _, row in df.iterrows()
                if row.get("id")
            }
    except Exception as e:
        print(f"  Warning: could not load email_memory sheet: {e}")
        _cache = {}
    return _cache


def is_seen(email_id: str) -> bool:
    return email_id in _load()


def get_company_history(company: str, n: int = 5) -> list[dict]:
    """Return the n most recent store records matching this company (for RAG context)."""
    if not company:
        return []
    norm = company.lower().strip()
    matches = [
        r for r in _load().values()
        if (c := (r.get("company") or "").lower().strip())
        and (norm in c or c in norm)
    ]
    matches.sort(key=lambda r: r.get("processed_at", ""), reverse=True)
    return matches[:n]


def save_records(records: list[dict]) -> None:
    if not records:
        return
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for r in records:
        row = {col: str(r.get(col, "") or "") for col in SCHEMA}
        if not row["processed_at"]:
            row["processed_at"] = now
        rows.append(row)

    if _USE_SHEETS:
        try:
            append_rows("email_memory", rows)
        except Exception as e:
            print(f"  Warning: could not save to email_memory sheet: {e}")

    # update in-memory cache so subsequent is_seen() calls reflect new records
    store = _load()
    for row in rows:
        if row.get("id"):
            store[row["id"]] = row


def extract_company_hint(sender: str, subject: str, snippet: str = "") -> str:
    """
    Best-effort company name extraction from email metadata, used to look up
    store history before the full AI summarize call.
    """
    from utils.filters import company_tier1, company_tier2
    text = f"{sender} {subject} {snippet}".lower()
    for company in list(company_tier1()) + list(company_tier2()):
        if re.search(rf"\b{re.escape(company.lower())}\b", text):
            return company

    # Fall back to sender domain (e.g. @stripe.com → stripe)
    m = re.search(r"@([a-z0-9-]+)\.", sender.lower())
    if m:
        domain = m.group(1)
        _generic = {
            "gmail", "yahoo", "hotmail", "outlook",
            # ATS platforms — domain is the vendor, not the hiring company
            "greenhouse", "lever", "myworkday", "workday", "taleo",
            "icims", "smartrecruiters", "bamboohr", "jobvite", "jazz",
            "successfactors", "rippling",
            # generic sending infrastructure
            "indeed", "linkedin", "glassdoor",
            "notify", "noreply", "no-reply", "mail", "notifications",
        }
        if domain not in _generic:
            return domain.replace("-", " ")
    return ""
