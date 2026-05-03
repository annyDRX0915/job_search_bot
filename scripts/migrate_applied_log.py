"""
One-time migration: backfill job_key column in the applied_log sheet.

Inserts job_key as the first column for all existing rows that don't have one.
Rows that already have a job_key are left untouched.

Usage:
    .venv/bin/python scripts/migrate_applied_log.py
"""

import hashlib
import os
import re
import sys
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.gsheets import read_sheet, write_sheet


def _make_job_key(title: str, company: str, location: str = "") -> str:
    def _n(s): return re.sub(r"[^a-z0-9]", "", (s or "").lower().strip())
    raw = f"{_n(title)}|{_n(company)}|{_n(location)}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def main():
    print("Reading applied_log sheet...")
    df = read_sheet("applied_log")

    if df.empty:
        print("Sheet is empty — nothing to migrate.")
        return

    print(f"  {len(df)} rows found. Columns: {list(df.columns)}")

    if "job_key" not in df.columns:
        df.insert(0, "job_key", "")

    missing = df["job_key"].isna() | (df["job_key"].astype(str).str.strip() == "")
    print(f"  {missing.sum()} rows missing job_key — backfilling...")

    df.loc[missing, "job_key"] = df.loc[missing].apply(
        lambda r: _make_job_key(
            r.get("title", ""),
            r.get("company", ""),
            r.get("location", ""),  # column absent in old rows → empty string
        ),
        axis=1,
    )

    # Ensure job_key is first column
    cols = ["job_key"] + [c for c in df.columns if c != "job_key"]
    df = df[cols]

    print("Writing back to sheet...")
    write_sheet("applied_log", df)
    print("Done. applied_log now has job_key as first column.")


if __name__ == "__main__":
    main()
