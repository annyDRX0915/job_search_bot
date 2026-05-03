"""
One-time migration: add interviewed and rejected columns to the applied_log sheet.

Inserts the columns after status, with empty values for all existing rows.
Safe to re-run — skips columns that already exist.

Usage:
    .venv/bin/python scripts/migrate_add_stage_columns.py
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.gsheets import read_sheet, write_sheet

NEW_COLS = ["interviewed", "rejected"]
# Target column order after migration
COL_ORDER = ["job_key", "url", "company", "title", "source", "status",
             "interviewed", "rejected", "timestamp", "notes"]


def main():
    print("Reading applied_log sheet...")
    df = read_sheet("applied_log")

    if df.empty:
        print("Sheet is empty — nothing to migrate.")
        return

    print(f"  {len(df)} rows, columns: {list(df.columns)}")

    already = [c for c in NEW_COLS if c in df.columns]
    if already:
        print(f"  Columns already present: {already} — skipping those.")

    for col in NEW_COLS:
        if col not in df.columns:
            df[col] = ""
            print(f"  Added column: {col}")

    # Reorder to canonical order; keep any unexpected extra columns at the end
    extra = [c for c in df.columns if c not in COL_ORDER]
    df = df[[c for c in COL_ORDER if c in df.columns] + extra]

    print("Writing back to sheet...")
    write_sheet("applied_log", df)
    print("Done.")
    print(f"Final columns: {list(df.columns)}")


if __name__ == "__main__":
    main()
