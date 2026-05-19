"""
Retry creating Apple Calendar events for email_memory rows that have an
event_datetime but no calendar_event_id (e.g. because the CalDAV call failed
during the original run).

Usage:
    .venv/bin/python scripts/retry_calendar_events.py
"""

import os
import sys
import uuid
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.gsheets import read_sheet, write_sheet
from utils.filters import calendar_name, assessment_reminder_hours


def _parse_event_dt(dt_str: str) -> datetime | None:
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


def _caldav_calendar():
    username = os.getenv("ICLOUD_USERNAME")
    password = os.getenv("ICLOUD_APP_PASSWORD")
    if not username or not password:
        raise SystemExit("ICLOUD_USERNAME / ICLOUD_APP_PASSWORD not set")
    import caldav
    target = calendar_name()
    client = caldav.DAVClient(
        url="https://caldav.icloud.com",
        username=username,
        password=password,
        timeout=15,
    )
    calendars = client.principal().calendars()
    for cal in calendars:
        try:
            if cal.get_display_name() == target:
                print(f"  Using calendar: {target}")
                return cal
        except Exception:
            continue
    print(f"  Calendar '{target}' not found — falling back to first")
    return calendars[0]


def _create_apple_event(calendar, title: str, start_dt: datetime,
                        duration_minutes: int = 60, description: str = "") -> str | None:
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
    ical_bytes = cal.to_ical()

    calendar_url = str(calendar.url).rstrip("/")
    event_url = f"{calendar_url}/{event_uid}.ics"
    username = os.getenv("ICLOUD_USERNAME")
    password = os.getenv("ICLOUD_APP_PASSWORD")

    for attempt in range(2):
        try:
            resp = requests.put(
                event_url,
                data=ical_bytes,
                auth=(username, password),
                headers={"Content-Type": "text/calendar; charset=utf-8"},
                timeout=15,
            )
            resp.raise_for_status()
            return event_uid
        except Exception as e:
            if attempt == 0:
                print(f"  Calendar write failed (retrying): {e}")
            else:
                print(f"  Calendar event creation failed: {e}")
    return None


def main():
    print("Reading email_memory from Google Sheets...")
    df = read_sheet("email_memory")
    if df.empty:
        print("  email_memory sheet is empty — nothing to retry")
        return

    for col in ("calendar_event_id", "event_datetime", "category", "company", "summary"):
        if col not in df.columns:
            df[col] = ""

    # Rows that need a calendar event: have event_datetime but no calendar_event_id
    mask = (
        df["event_datetime"].str.strip().astype(bool) &
        ~df["calendar_event_id"].str.strip().astype(bool)
    )
    pending = df[mask].copy()
    print(f"  {len(pending)} row(s) with event_datetime but no calendar_event_id")

    if pending.empty:
        print("  Nothing to retry.")
        return

    print("Connecting to Apple Calendar...")
    calendar = _caldav_calendar()
    reminder_hours = assessment_reminder_hours()

    updated = 0
    for idx, row in pending.iterrows():
        company = row.get("company") or "Unknown"
        category = row.get("category", "")
        raw_dt = str(row.get("event_datetime", "")).strip()
        summary = str(row.get("summary", ""))

        event_dt = _parse_event_dt(raw_dt)
        if not event_dt:
            print(f"  ⚠️  Could not parse datetime {raw_dt!r} for {company} — skipping")
            continue

        # Use same offset logic as email_agent: assessment deadlines get a reminder offset
        is_assessment = "deadline" in summary.lower() or "complete" in summary.lower()
        if is_assessment and category == "interview":
            event_dt = event_dt - timedelta(hours=reminder_hours)

        title = f"Interview @ {company}" if category == "interview" else f"Follow-up @ {company}"
        uid = _create_apple_event(
            calendar,
            title=title,
            start_dt=event_dt,
            duration_minutes=60,
            description=summary,
        )
        if uid:
            df.at[idx, "calendar_event_id"] = uid
            updated += 1
            print(f"  📅 Created: {title} at {raw_dt}")
        else:
            print(f"  ✗  Failed: {title}")

    if updated:
        print(f"\nWriting {updated} updated row(s) back to email_memory sheet...")
        write_sheet("email_memory", df)
        print("Done.")
    else:
        print("No events created.")


if __name__ == "__main__":
    main()
