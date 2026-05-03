"""
Email agent — reads all last-24h Gmail, triages with AI, summarizes actionable
emails, updates applied_log stage columns, and posts a digest to Discord.

Usage:
    .venv/bin/python agents/email_agent.py
"""

import base64
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
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


# ── email helpers ─────────────────────────────────────────────────────────────

def _header(headers: list, name: str) -> str:
    return next((h["value"] for h in headers if h["name"].lower() == name.lower()), "")


def _extract_text(payload: dict) -> str:
    """Recursively extract readable text from email payload, preferring HTML."""
    mime = payload.get("mimeType", "")
    if mime == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            return BeautifulSoup(html, "html.parser").get_text(separator=" ", strip=True)
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
        params = {"userId": "me", "q": "newer_than:1d", "maxResults": 500}
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
    """True if this sender is already handled by crawler_email.py."""
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
    """Append newly AI-flagged sender addresses to spam_senders.json."""
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
        "- interview: human-initiated interview invite, technical assessment, or scheduling request\n"
        "- rejection: company telling you they won't proceed — declined, not moving forward, position no longer active, not selected, chosen another candidate\n"
        "- offer: job offer or compensation discussion\n"
        "- job_application: automated confirmation a specific application was received or viewed\n"
        "- important: email from a SPECIFIC real person who knows you — career counselor, recruiter you have an ongoing conversation with, friend, manager. Must be a genuine personal exchange, NOT mass outreach.\n"
        "- job_alert: automated digest listing multiple job postings\n"
        "- newsletter: marketing content, success stories, tips, 'how to get a job' articles, calls to book a call with a stranger\n"
        "- notification: automated system messages — verification codes, appointment confirmations, LinkedIn system emails, automated 'complete your profile/interview/application' reminders, cold recruiter outreach to many candidates\n"
        "- spam: unsolicited bulk promotional or sales email\n\n"
        "Key rules:\n"
        "- Cold recruiter mass outreach → notification, NOT important\n"
        "- Automated 'complete your interview' reminders → notification\n"
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
        # Response was truncated — split batch and retry
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

def summarize_email(email: dict, body_text: str) -> dict:
    prompt = (
        'Summarize this email. Return JSON:\n'
        '{"summary": "1-2 sentence summary", "action": "recommended next step or empty string",'
        ' "company": "company name if job-related else empty", "title": "job title if job-related else empty"}\n\n'
        f"Subject: {email['subject']}\nFrom: {email['sender']}\n\n{body_text[:3000]}"
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
            "category": email.get("category", ""),
            "summary": data.get("summary", ""),
            "action": data.get("action", ""),
            "company": data.get("company", ""),
            "title": data.get("title", ""),
        }
    except Exception as e:
        print(f"  Summarize failed for '{email['subject'][:50]}': {e}")
        return {
            "id": email["id"],
            "subject": email["subject"],
            "sender": email["sender"],
            "category": email.get("category", ""),
            "summary": email["snippet"],
            "action": "",
            "company": "",
            "title": "",
        }


# ── applied log update ────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower().strip())


def update_applied_log(summaries: list[dict]) -> None:
    """Set interviewed/rejected on the best-matching applied_log row."""
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

        # Company match: either string is a substring of the other
        mask = df["_cn"].apply(lambda c: bool(c and (company in c or c in company)))

        # Narrow by title if available and it improves the match
        if title and mask.sum() > 1:
            title_mask = df["_tn"].apply(lambda t: bool(t and title[:12] in t))
            if (mask & title_mask).any():
                mask = mask & title_mask

        if not mask.any():
            print(f"  No match in applied_log for: {s.get('company')} — {s.get('title')}")
            continue

        idx = df[mask].index[-1]  # most recent matching row
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
    ("interview",      "🔴 ACTION REQUIRED"),
    ("offer",          "🟢 OFFERS"),
    ("rejection",      "🟡 REJECTIONS"),
    ("job_application","🔵 APPLICATION UPDATES"),
    ("important",      "⚪ OTHER IMPORTANT"),
]


def _build_digest(summaries: list[dict], filtered_by_cat: dict[str, int]) -> list[str]:
    """Build Discord message(s), each ≤1900 chars."""
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

    total_filtered = sum(filtered_by_cat.values())
    if total_filtered:
        breakdown = ", ".join(
            f"{count} {cat}" for cat, count in sorted(filtered_by_cat.items()) if count
        )
        lines.append(f"\n⚫ {total_filtered} filtered — {breakdown}")

    # Chunk into ≤1900 char messages for Discord
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

    total_filtered = sum(filtered_by_cat.values())
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

    # 2. Pre-filter: job alert senders (already parsed by crawler_email.py) + static spam
    spam_config = _load_spam_config()
    survivors, filtered_by_cat = [], defaultdict(int)
    for e in all_emails:
        if _is_job_alert(e["sender"]):
            filtered_by_cat["job_alerts (crawler)"] += 1
        elif _is_static_spam(e["sender"], e["subject"], spam_config):
            filtered_by_cat["static spam"] += 1
        else:
            survivors.append(e)
    pre_filtered = len(all_emails) - len(survivors)
    print(f"  {pre_filtered} removed by pre-filter → {len(survivors)} to AI triage")

    # 3. AI triage
    print("Triaging with AI...")
    categories = triage_emails(survivors)
    for e in survivors:
        e["category"] = categories.get(e["id"], "notification")

    # 4. Learn new spam senders from AI-flagged emails
    new_spam = [e["sender"] for e in survivors if e["category"] in ("spam", "newsletter")]
    if new_spam:
        _learn_spam_senders(new_spam, spam_config)

    for e in survivors:
        if e["category"] in FILTER_CATS:
            filtered_by_cat[e["category"]] += 1

    actionable = [e for e in survivors if e["category"] in SUMMARIZE_CATS]
    ai_filtered = sum(filtered_by_cat[c] for c in FILTER_CATS)
    print(f"  {ai_filtered} filtered by AI → {len(actionable)} to summarize")

    # 5. Fetch full body and summarize each actionable email
    summaries = []
    if actionable:
        print(f"Summarizing {len(actionable)} emails...")
        for e in actionable:
            try:
                full = service.users().messages().get(
                    userId="me", id=e["id"], format="full"
                ).execute()
                body = _extract_text(full["payload"])[:3000]
            except Exception:
                body = e["snippet"]
            summary = summarize_email(e, body)
            summaries.append(summary)
            label = summary.get("company") or _sender_name(summary["sender"])
            print(f"  [{summary['category'].upper():14}] {label} — {summary['subject'][:50]}")

    # 6. Update applied_log for interviews and rejections
    _REJECTION_PHRASES = ("not move forward", "not selected", "not retained", "position no longer",
                          "decided not to", "unsuccessful", "chosen another", "not a match",
                          "not be moving", "not be proceeding")
    for s in summaries:
        if s["category"] == "important":
            text = (s.get("summary", "") + " " + s.get("subject", "")).lower()
            if any(p in text for p in _REJECTION_PHRASES):
                s["category"] = "rejection"

    log_updates = [s for s in summaries if s["category"] in UPDATE_LOG_CATS]
    if log_updates:
        print("Updating applied_log...")
        update_applied_log(log_updates)

    # 7. Build and post Discord digest
    messages = _build_digest(summaries, filtered_by_cat)
    print("\n--- Discord Digest Preview ---")
    for m in messages:
        print(m)
    print("------------------------------")
    post_to_discord(messages)
    print("Done.")


if __name__ == "__main__":
    main()
