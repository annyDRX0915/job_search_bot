"""
Google Sheets helper — read, write, and append tabs in the job-search spreadsheet.

Auth (checked in order):
  GOOGLE_SERVICE_ACCOUNT_JSON      — JSON string (GitHub Actions secret)
  GOOGLE_SERVICE_ACCOUNT_JSON_PATH — path to key file (local dev)
"""

import json
import os

import gspread
import pandas as pd
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _client() -> tuple[gspread.Spreadsheet, str]:
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    if not spreadsheet_id:
        raise RuntimeError("SPREADSHEET_ID env var not set")

    json_str = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if json_str:
        creds = Credentials.from_service_account_info(json.loads(json_str), scopes=SCOPES)
    else:
        path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_PATH")
        if not path:
            raise RuntimeError(
                "Set GOOGLE_SERVICE_ACCOUNT_JSON (JSON string) or "
                "GOOGLE_SERVICE_ACCOUNT_JSON_PATH (file path)"
            )
        creds = Credentials.from_service_account_file(path, scopes=SCOPES)

    return gspread.authorize(creds).open_by_key(spreadsheet_id)


def read_sheet(tab: str) -> pd.DataFrame:
    """Read a tab and return a DataFrame. Returns empty DataFrame if tab doesn't exist."""
    try:
        ws = _client().worksheet(tab)
        records = ws.get_all_records()
        return pd.DataFrame(records)
    except gspread.WorksheetNotFound:
        return pd.DataFrame()


def write_sheet(tab: str, df: pd.DataFrame) -> None:
    """Clear a tab and write the full DataFrame. Creates the tab if it doesn't exist."""
    sh = _client()
    try:
        ws = sh.worksheet(tab)
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab, rows=1000, cols=40)

    df = df.fillna("").astype(str)
    ws.update([df.columns.tolist()] + df.values.tolist())


def append_rows(tab: str, rows: list[dict]) -> None:
    """Append rows to a tab, aligning values to the existing header order."""
    if not rows:
        return
    sh = _client()
    try:
        ws = sh.worksheet(tab)
        existing = ws.get_all_values()
        if existing:
            headers = existing[0]
        else:
            headers = list(rows[0].keys())
            ws.append_row(headers)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab, rows=1000, cols=40)
        headers = list(rows[0].keys())
        ws.append_row(headers)

    for row in rows:
        ws.append_row([str(row.get(h, "")) for h in headers])
