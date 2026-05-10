"""
Email agent — reads all last-24h Gmail, triages with AI, summarizes actionable
emails, updates applied_log stage columns, creates Apple Calendar events for
interviews/assessments, and posts a digest to Discord.

Usage:
    .venv/bin/python agents/email_agent.py
"""

import base64
import json
import os
import re
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from openai import OpenAI

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent.parent))

_USE_SHEETS = bool(os.getenv("SPREADSHEET_ID"))
if _USE_SHEETS:
    from utils.gsheets import read_sheet, write_sheet

from agents.email_store import (
    is_seen, get_company_history, save_records, extract_company_hint,
)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
SPAM_FILE = Path(__file__).parent / "spam_senders.json"

# Same senders already parsed by crawler/crawler_email.py — skip before AI triage
JOB_ALERT_SENDERS = {
    "jobalerts-noreply@linkedin.com",
    "donotreply@jobalert.indeed.com",
    "donotreply@match.indeed.com",
    "noreply@glassdoor.com",
    "alert@indeed.com",
}

SUMMARIZE_CATS = {"interview", "rejection", "offer", "important", "job_application"}
UPDATE_LOG_CATS = {"interview", "rejection"}
FILTER_CATS    = {"spam", "newsletter", "notification", "job_alert"}

# Categories where we attempt calendar event creation
CALENDAR_CATS = {"interview", "important"}


# ── clients ───────────────────────────────────────────────────────────────────

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


def _oai() -> OpenAI:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise SystemExit("OPENAI_API_KEY not set in .env")
    return OpenAI(api_key=key)


# ── Apple Calendar (iCloud CalDAV) ────────────────────────────────────────────

def _caldav_calendar():
    """Connect to iCloud CalDAV and return the configured calendar by name."""
    username = os.getenv("ICLOUD_USERNAME")
    password = os.getenv("ICLOUD_APP_PASSWORD")
    if not username or not password:
        print("  ICLOUD_USERNAME / ICLOUD_APP_PASSWORD not set — skipping calendar")
        return None
    try:
        import caldav
        from utils.filters import calendar_name
        target = calendar_name()
        client = caldav.DAVClient(
            url="https://caldav.icloud.com",
            username=username,
            password=password,
            timeout=15,
        )
        calendars = client.principal().calendars()
        if not calendars:
            print("  No calendars found in iCloud account")
            return None
        for cal in calendars:
            try:
                if cal.get_display_name() == target:
                    print(f"  Using calendar: {target}")
                    return cal
            except Exception:
                continue
        print(f"  Calendar '{target}' not found — available: {[c.get_display_name() for c in calendars]}")
        print(f"  Falling back to first calendar")
        return calendars[0]
    except Exception as e:
        print(f"  iCloud CalDAV connect failed: {e}")
        return None


def _parse_event_dt(dt_str: str) -> datetime | None:
    """Parse an ISO8601 string returned by the AI into a timezone-aware datetime."""
    if not dt_str:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(dt_str.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def _create_apple_event(
    calendar,
    title: str,
    start_dt: datetime,
    duration_minutes: int = 60,
    description: str = "",
) -> str | None:
    """Create an event in iCloud Calendar. Returns the event UID or None on failure."""
    from icalendar import Calendar, Event

    cal = Calendar()
    cal.add("prodid", "-//job-search-bot//EN")
    cal.add("version", "2.0")

    event = Event()
    event.add("summary", title)
    event.add("dtstart", start_dt)
    event.add("dtend", start_dt + timedelta(minutes=duration_minutes))
    if description:
        event.add("description", description)
    event_uid = str(uuid.uuid4())
    event.add("uid", event_uid)
    cal.add_component(event)
    ical_data = cal.to_ical().decode()

    for attempt in range(2):
        try:
            calendar.add_event(ical_data)
            return event_uid
        except Exception as e:
            if attempt == 0:
                print(f"  Calendar write failed (retrying): {e}")
            else:
                print(f"  Calendar event creation failed after retry: {e}")
    return None


# ── email helpers ─────────────────────────────────────────────────────────────

def _header(headers: list, name: str) -> str:
    return next((h["value"] for h in headers if h["name"].lower() == name.lower()), "")


_ATS_DOMAINS = {
    "myworkday.com", "greenhouse.io", "lever.co", "taleo.net",
    "icims.com", "smartrecruiters.com", "bamboohr.com", "jobvite.com",
    "successfactors.com", "rippling.com", "jazz.co",
}


def _sender_for_ai(sender: str) -> str:
    """Mask ATS platform emails so the AI doesn't use the local part as company name."""
    m = re.search(r"<([^>]+)>", sender)
    email = (m.group(1) if m else sender).strip().lower()
    domain = email.split("@")[-1] if "@" in email else ""
    if domain in _ATS_DOMAINS:
        return f"[sent via {domain} — ignore sender for company name]"
    return sender


def _extract_text(payload: dict) -> str:
    """Recursively extract readable text from email payload, preferring HTML.
    Also appends img alt text so logo-only company names are captured."""
    mime = payload.get("mimeType", "")
    if mime == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            soup = BeautifulSoup(html, "html.parser")
            text = soup.get_text(separator=" ", strip=True)
            # img alt attributes (company logos often only appear here)
            alts = [
                img["alt"].strip()
                for img in soup.find_all("img", alt=True)
                if len(img["alt"].strip()) > 2
            ]
            if alts:
                text += " " + " ".join(alts)
            return text
        return ""
    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace") if data else ""
    if mime.startswith("multipart/"):
        for part in payload.get("parts", []):
            if part.get("mimeType") == "text/html":
                return _extract_text(part)
        for part in payload.get("parts", []):
            result = _extract_text(part)
            if result:
                return result
    return ""


def _sender_name(sender: str) -> str:
    m = re.match(r"^([^<]+)<", sender)
    return m.group(1).strip() if m else sender.split("@")[0]


# ── fetch ─────────────────────────────────────────────────────────────────────

def _fetch_meta(service, msg_id: str) -> dict:
    meta = service.users().messages().get(
        userId="me", id=msg_id, format="metadata",
        metadataHeaders=["subject", "from"],
    ).execute()
    hdrs = meta["payload"].get("headers", [])
    return {
        "id": msg_id,
        "subject": _header(hdrs, "subject"),
        "sender": _header(hdrs, "from"),
        "snippet": meta.get("snippet", ""),
    }


def fetch_all_emails(service) -> list[dict]:
    """Fetch metadata for all emails from last 24h (handles pagination)."""
    msg_ids, page_token = [], None
    while True:
        params = {"userId": "me", "q": "newer_than:7d", "maxResults": 500}
        if page_token:
            params["pageToken"] = page_token
        resp = service.users().messages().list(**params).execute()
        msg_ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    print(f"  Fetching metadata for {len(msg_ids)} emails...")
    emails = []
    for mid in msg_ids:
        try:
            emails.append(_fetch_meta(service, mid))
        except Exception as e:
            print(f"  Warning: skipping {mid}: {e}")
    return emails


# ── spam filter ───────────────────────────────────────────────────────────────

def _load_spam_config() -> dict:
    if SPAM_FILE.exists():
        return json.loads(SPAM_FILE.read_text())
    return {"domains": [], "subject_patterns": [], "learned_senders": []}


def _is_job_alert(sender: str) -> bool:
    s = sender.lower()
    return any(addr in s for addr in JOB_ALERT_SENDERS)


def _is_static_spam(sender: str, subject: str, config: dict) -> bool:
    s = sender.lower()
    subj = subject.lower()
    for domain in config.get("domains", []):
        if domain.lower() in s:
            return True
    for addr in config.get("learned_senders", []):
        if addr.lower() in s:
            return True
    for pattern in config.get("subject_patterns", []):
        if re.search(pattern, subj, re.IGNORECASE):
            return True
    return False


def _learn_spam_senders(senders: list[str], config: dict) -> None:
    existing = set(config.get("learned_senders", []))
    added = []
    for sender in senders:
        m = re.search(r"<([^>]+)>", sender)
        addr = (m.group(1) if m else sender).strip().lower()
        if addr and addr not in existing:
            existing.add(addr)
            added.append(addr)
    if added:
        config["learned_senders"] = sorted(existing)
        SPAM_FILE.write_text(json.dumps(config, indent=2))
        print(f"  Learned {len(added)} new spam sender(s): {added[:3]}")


# ── AI triage ─────────────────────────────────────────────────────────────────

def _triage_batch(emails: list[dict]) -> dict[str, str]:
    payload = [
        {
            "id": e["id"],
            "from": e["sender"][:80],
            "subject": e["subject"][:120],
            "snippet": e["snippet"][:300],
        }
        for e in emails
    ]
    prompt = (
        'Categorize each email. Return JSON: {"results": [{"id": "...", "category": "..."}]}\n\n'
        "Categories (match the FIRST that fits):\n"
        "- interview: human-initiated interview invite, technical assessment, OR a link/invitation to complete a coding test on platforms like Codility, HackerRank, LeetCode, Coderbyte, TestGorilla, or similar. This includes automated emails delivering a test link on behalf of a company that is moving you forward.\n"
        "- rejection: company telling you they won't proceed — declined, not moving forward, position no longer active, not selected, chosen another candidate\n"
        "- offer: job offer or compensation discussion\n"
        "- job_application: automated confirmation a specific application was received or viewed\n"
        "- important: email from a SPECIFIC real person who knows you — career counselor, recruiter you have an ongoing conversation with, friend, manager. Must be a genuine personal exchange, NOT mass outreach.\n"
        "- job_alert: automated digest listing multiple job postings\n"
        "- newsletter: marketing content, success stories, tips, 'how to get a job' articles, calls to book a call with a stranger\n"
        "- notification: automated system messages — verification codes, appointment confirmations, LinkedIn system emails, generic 'complete your profile' reminders, cold recruiter outreach to many candidates\n"
        "- spam: unsolicited bulk promotional or sales email\n\n"
        "Key rules:\n"
        "- Codility / HackerRank / coding test links → interview (NOT notification), even if sent by an automated system\n"
        "- Cold recruiter mass outreach → notification, NOT important\n"
        "- Verification codes → notification\n"
        "- 'Success story' or tips content → newsletter\n"
        "- important ONLY when a named individual is clearly writing to YOU specifically\n\n"
        "Emails:\n" + json.dumps(payload)
    )
    try:
        resp = _oai().chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=2000,
        )
        data = json.loads(resp.choices[0].message.content)
        return {r["id"]: r["category"] for r in data.get("results", [])}
    except json.JSONDecodeError:
        if len(emails) > 5:
            mid = len(emails) // 2
            result = _triage_batch(emails[:mid])
            result.update(_triage_batch(emails[mid:]))
            return result
        return {}
    except Exception as e:
        print(f"  Triage batch failed: {e}")
        return {}


def triage_emails(emails: list[dict]) -> dict[str, str]:
    categories = {}
    batch_size = 100
    total_batches = (len(emails) - 1) // batch_size + 1
    for i in range(0, len(emails), batch_size):
        batch = emails[i:i + batch_size]
        print(f"  Triage batch {i // batch_size + 1}/{total_batches} ({len(batch)} emails)...")
        categories.update(_triage_batch(batch))
    return categories


# ── AI summarize ──────────────────────────────────────────────────────────────

def summarize_email(email: dict, body_text: str, company_history: list[dict] | None = None) -> dict:
    category = email.get("category", "")
    needs_event = category in CALENDAR_CATS

    event_schema = ""
    if needs_event:
        today = datetime.now().strftime("%Y-%m-%d")
        event_schema = (
            ', "event_type": "interview | assessment_deadline | null"'
            f', "event_datetime": "ISO8601 datetime string or null. Today is {today}.'
            " For assessment deadlines: if a specific date is given use it, if relative ('5 days from now') calculate the actual date."
            " If no time is given, use 23:00. Format: YYYY-MM-DDTHH:MM:00."
            ' If truly no date or deadline can be found, use null."'
            ', "event_duration_minutes": 60'
            ', "event_title": "short display title for calendar e.g. Interview @ Stripe"'
        )

    history_block = ""
    if company_history:
        lines = [
            f"- {r.get('category', '?')} on {str(r.get('processed_at', ''))[:10]}: {str(r.get('summary', ''))[:100]}"
            for r in company_history
        ]
        history_block = "\n\nPrevious emails from this company:\n" + "\n".join(lines)

    prompt = (
        "Summarize this email. Return JSON:\n"
        '{"summary": "1-2 sentence summary", "action": "recommended next step or empty string",'
        ' "company": "hiring company name — extract from the email BODY, footer, logo text, or signature.'
        " Do NOT use the sender email address or ATS domain (myworkday.com, greenhouse.io, lever.co, etc.)."
        ' Look for the company name in the sign-off, logo, or anywhere in the body. Empty string if not job-related.",'
        ' "title": "job title if job-related else empty"'
        f"{event_schema}}}\n\n"
        f"Subject: {email['subject']}\nFrom: {_sender_for_ai(email['sender'])}\n\n{body_text[:3000]}"
        f"{history_block}"
    )
    try:
        resp = _oai().chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0,
        )
        data = json.loads(resp.choices[0].message.content)
        return {
            "id": email["id"],
            "subject": email["subject"],
            "sender": email["sender"],
            "category": category,
            "summary": data.get("summary", ""),
            "action": data.get("action", ""),
            "company": data.get("company", ""),
            "title": data.get("title", ""),
            "event_type": data.get("event_type") or "",
            "event_datetime": data.get("event_datetime") or "",
            "event_duration_minutes": int(data.get("event_duration_minutes") or 60),
            "event_title": data.get("event_title") or "",
            "calendar_event_id": "",
        }
    except Exception as e:
        print(f"  Summarize failed for '{email['subject'][:50]}': {e}")
        return {
            "id": email["id"],
            "subject": email["subject"],
            "sender": email["sender"],
            "category": category,
            "summary": email["snippet"],
            "action": "",
            "company": "",
            "title": "",
            "event_type": "",
            "event_datetime": "",
            "event_duration_minutes": 60,
            "event_title": "",
            "calendar_event_id": "",
        }


# ── applied log update ────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower().strip())


def update_applied_log(summaries: list[dict]) -> None:
    if not _USE_SHEETS:
        print("  Skipping applied_log update (SPREADSHEET_ID not set)")
        return

    df = read_sheet("applied_log")
    if df.empty:
        return

    for col in ("interviewed", "rejected"):
        if col not in df.columns:
            df[col] = ""

    df["_cn"] = df["company"].apply(_norm)
    df["_tn"] = df["title"].apply(_norm)

    updated = 0
    for s in summaries:
        company = _norm(s.get("company", ""))
        title   = _norm(s.get("title", ""))
        cat     = s["category"]

        if not company:
            continue

        mask = df["_cn"].apply(lambda c: bool(c and (company in c or c in company)))

        if title and mask.sum() > 1:
            title_mask = df["_tn"].apply(lambda t: bool(t and title[:12] in t))
            if (mask & title_mask).any():
                mask = mask & title_mask

        if not mask.any():
            print(f"  No match in applied_log for: {s.get('company')} — {s.get('title')}")
            continue

        idx = df[mask].index[-1]
        if cat == "interview":
            df.at[idx, "interviewed"] = "interviewed"
            print(f"  Marked interviewed: {df.at[idx, 'company']} — {df.at[idx, 'title']}")
        elif cat == "rejection":
            df.at[idx, "rejected"] = "rejected"
            print(f"  Marked rejected: {df.at[idx, 'company']} — {df.at[idx, 'title']}")
        updated += 1

    df = df.drop(columns=["_cn", "_tn"])
    if updated:
        write_sheet("applied_log", df)
        print(f"  Updated {updated} row(s) in applied_log")


# ── Discord ───────────────────────────────────────────────────────────────────

_SECTION_ORDER = [
    ("interview",       "🔴 ACTION REQUIRED"),
    ("offer",           "🟢 OFFERS"),
    ("rejection",       "🟡 REJECTIONS"),
    ("job_application", "🔵 APPLICATION UPDATES"),
    ("important",       "⚪ OTHER IMPORTANT"),
]


def _build_digest(summaries: list[dict], filtered_by_cat: dict[str, int]) -> list[str]:
    today = datetime.now().strftime("%b %-d")
    lines = [f"📬 **Daily Email Digest — {today}**\n"]

    by_cat = defaultdict(list)
    for s in summaries:
        by_cat[s["category"]].append(s)

    for cat, header in _SECTION_ORDER:
        items = by_cat.get(cat, [])
        if not items:
            continue
        cap = 5 if cat == "job_application" else None
        shown, overflow = items[:cap], items[cap:]
        label = f"{header} ({len(items)})" + (f" — showing {len(shown)}" if overflow else "")
        lines.append(f"\n**{label}**")
        for item in shown:
            company = item.get("company") or _sender_name(item["sender"])
            title_part = f" · {item['title']}" if item.get("title") else ""
            lines.append(f"• **{company}{title_part}** — {item['summary']}")
            if item.get("action"):
                lines.append(f"  → {item['action']}")
            # Calendar status for interview / assessment emails
            if item.get("calendar_event_id"):
                if item.get("event_type") == "assessment_deadline":
                    from utils.filters import assessment_reminder_hours
                    h = assessment_reminder_hours()
                    dt_label = str(item.get("event_datetime", ""))[:10]
                    lines.append(f"  📅 Reminder set {h}h before deadline ({dt_label})")
                else:
                    lines.append(f"  📅 Added to Apple Calendar")
            elif cat in ("interview",) and item.get("event_type"):
                lines.append(f"  ⚠️ No time found — add to calendar manually")

    total_filtered = sum(filtered_by_cat.values())
    if total_filtered:
        breakdown = ", ".join(
            f"{count} {cat}" for cat, count in sorted(filtered_by_cat.items()) if count
        )
        lines.append(f"\n⚫ {total_filtered} filtered — {breakdown}")

    messages, current = [], ""
    for line in lines:
        candidate = current + line + "\n"
        if len(candidate) > 1900:
            if current.strip():
                messages.append(current.strip())
            current = line + "\n"
        else:
            current = candidate
    if current.strip():
        messages.append(current.strip())

    return messages or [f"📬 **Daily Email Digest — {today}**\n\n⚫ {total_filtered} emails filtered — nothing actionable today."]


def post_to_discord(messages: list[str]) -> None:
    url = os.getenv("DISCORD_WEBHOOK_URL")
    if not url:
        print("DISCORD_WEBHOOK_URL not set — skipping Discord post")
        return
    for msg in messages:
        try:
            resp = requests.post(url, json={"content": msg}, timeout=10)
            resp.raise_for_status()
            print(f"  Posted to Discord ({len(msg)} chars)")
        except Exception as e:
            print(f"  Discord post failed: {e}")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    service = _gmail_service()
    print("Gmail auth OK")

    # 1. Fetch all emails from last 24h
    print("Fetching emails from last 24h...")
    all_emails = fetch_all_emails(service)
    print(f"  {len(all_emails)} total emails")

    if not all_emails:
        post_to_discord(["📬 No emails in the last 24h."])
        return

    # 2. Filter already-seen emails (de-duplication across runs)
    unseen = [e for e in all_emails if not is_seen(e["id"])]
    already_seen = len(all_emails) - len(unseen)
    if already_seen:
        print(f"  {already_seen} already processed in a previous run → {len(unseen)} new")
    if not unseen:
        post_to_discord(["📬 No new emails since last run."])
        return

    # 3. Pre-filter: job alert senders + static spam
    spam_config = _load_spam_config()
    survivors, filtered_by_cat = [], defaultdict(int)
    for e in unseen:
        if _is_job_alert(e["sender"]):
            filtered_by_cat["job_alerts (crawler)"] += 1
        elif _is_static_spam(e["sender"], e["subject"], spam_config):
            filtered_by_cat["static spam"] += 1
        else:
            survivors.append(e)
    print(f"  {len(unseen) - len(survivors)} removed by pre-filter → {len(survivors)} to AI triage")

    # 4. AI triage
    print("Triaging with AI...")
    categories = triage_emails(survivors)
    for e in survivors:
        e["category"] = categories.get(e["id"], "notification")

    # 5. Learn new spam senders
    new_spam = [e["sender"] for e in survivors if e["category"] in ("spam", "newsletter")]
    if new_spam:
        _learn_spam_senders(new_spam, spam_config)

    for e in survivors:
        if e["category"] in FILTER_CATS:
            filtered_by_cat[e["category"]] += 1

    actionable = [e for e in survivors if e["category"] in SUMMARIZE_CATS]
    ai_filtered = sum(filtered_by_cat[c] for c in FILTER_CATS)
    print(f"  {ai_filtered} filtered by AI → {len(actionable)} to summarize")

    # 6. Connect to Apple Calendar once (shared across all events this run)
    calendar = _caldav_calendar() if actionable else None

    # 7. Fetch full body, inject RAG history, summarize, create calendar events
    summaries = []
    if actionable:
        from utils.filters import assessment_reminder_hours
        reminder_hours = assessment_reminder_hours()

        print(f"Summarizing {len(actionable)} emails...")
        for e in actionable:
            try:
                full = service.users().messages().get(
                    userId="me", id=e["id"], format="full"
                ).execute()
                body = _extract_text(full["payload"])[:3000]
            except Exception:
                body = e["snippet"]

            # RAG: look up company history from store before summarizing
            company_hint = extract_company_hint(e["sender"], e["subject"], e["snippet"])
            history = get_company_history(company_hint) if company_hint else []

            summary = summarize_email(e, body, company_history=history or None)

            label = summary.get("company") or _sender_name(summary["sender"])
            print(f"  [{summary['category'].upper():14}] {label} — {summary['subject'][:50]}")

            # Calendar: create event for interview / assessment emails
            if calendar and summary.get("event_type") and summary["event_type"] != "null":
                raw_dt = summary.get("event_datetime", "")
                event_dt = _parse_event_dt(raw_dt)
                if event_dt:
                    if summary["event_type"] == "assessment_deadline":
                        event_dt = event_dt - timedelta(hours=reminder_hours)
                    uid = _create_apple_event(
                        calendar,
                        title=summary.get("event_title") or f"Interview @ {label}",
                        start_dt=event_dt,
                        duration_minutes=summary.get("event_duration_minutes") or 60,
                        description=summary.get("summary", ""),
                    )
                    if uid:
                        summary["calendar_event_id"] = uid
                        print(f"  📅 Calendar event created: {summary.get('event_title', label)}")
                else:
                    print(f"  ⚠️  Could not parse event datetime: {raw_dt!r}")

            summaries.append(summary)

    # 8. Reclassify important→rejection if summary contains rejection phrases
    _REJECTION_PHRASES = (
        "not move forward", "not selected", "not retained", "position no longer",
        "decided not to", "unsuccessful", "chosen another", "not a match",
        "not be moving", "not be proceeding",
    )
    for s in summaries:
        if s["category"] == "important":
            text = (s.get("summary", "") + " " + s.get("subject", "")).lower()
            if any(p in text for p in _REJECTION_PHRASES):
                s["category"] = "rejection"

    # 9. Update applied_log for interviews and rejections
    log_updates = [s for s in summaries if s["category"] in UPDATE_LOG_CATS]
    if log_updates:
        print("Updating applied_log...")
        update_applied_log(log_updates)

    # 10. Persist all processed emails to email_memory store
    print("Saving to email_memory...")
    save_records(summaries)
    # Also record non-actionable survivors so they're not re-processed next run
    non_actionable = [
        {
            "id": e["id"],
            "category": e.get("category", "notification"),
            "company": "",
            "title": "",
            "summary": e.get("snippet", "")[:120],
            "action": "",
        }
        for e in survivors if e["category"] not in SUMMARIZE_CATS
    ]
    save_records(non_actionable)

    # 11. Build and post Discord digest
    messages = _build_digest(summaries, filtered_by_cat)
    print("\n--- Discord Digest Preview ---")
    for m in messages:
        print(m)
    print("------------------------------")
    post_to_discord(messages)
    print("Done.")


if __name__ == "__main__":
    main()
