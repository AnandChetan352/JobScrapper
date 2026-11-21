from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
import pandas as pd

DB_PATH = "jobs_history.db"
STATUS_OPTIONS = ["New", "Saved", "Applied", "Interview", "Offer", "Rejected", "Ignored"]


def init_db(db_path: str = DB_PATH) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_tracker (
                job_id TEXT PRIMARY KEY,
                title TEXT,
                company TEXT,
                source TEXT,
                est_annual_inr REAL,
                apply_url TEXT,
                first_seen_utc TEXT,
                last_seen_utc TEXT,
                app_status TEXT DEFAULT 'New',
                notes TEXT DEFAULT ''
            )
            """
        )
        conn.commit()


def sync_jobs_with_db(df: pd.DataFrame, db_path: str = DB_PATH) -> pd.DataFrame:
    """Upserts scraped jobs into SQLite and attaches is_new, app_status, and notes columns."""
    if df.empty:
        return df

    init_db(db_path)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    with sqlite3.connect(db_path) as conn:
        existing = pd.read_sql_query("SELECT job_id, app_status, notes, first_seen_utc FROM job_tracker", conn)
        existing_map = existing.set_index("job_id").to_dict(orient="index")

        is_new_flags = []
        statuses = []
        notes_list = []

        for _, row in df.iterrows():
            jid = row["job_id"]
            if jid in existing_map:
                is_new_flags.append(False)
                statuses.append(existing_map[jid]["app_status"] or "New")
                notes_list.append(existing_map[jid]["notes"] or "")
                conn.execute(
                    "UPDATE job_tracker SET last_seen_utc = ?, est_annual_inr = ? WHERE job_id = ?",
                    (now, row["est_annual_inr"] if pd.notna(row["est_annual_inr"]) else None, jid),
                )
            else:
                is_new_flags.append(True)
                statuses.append("New")
                notes_list.append("")
                conn.execute(
                    """
                    INSERT INTO job_tracker (job_id, title, company, source, est_annual_inr, apply_url, first_seen_utc, last_seen_utc)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        jid,
                        row["title"],
                        row["company"],
                        row["source"],
                        row["est_annual_inr"] if pd.notna(row["est_annual_inr"]) else None,
                        row["apply_url"],
                        now,
                        now,
                    ),
                )
        conn.commit()

    out = df.copy()
    out["is_new"] = is_new_flags
    out["app_status"] = statuses
    out["notes"] = notes_list
    return out


def update_job_status(job_id: str, app_status: str, notes: str, db_path: str = DB_PATH) -> None:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE job_tracker SET app_status = ?, notes = ? WHERE job_id = ?",
            (app_status, notes, job_id),
        )
        conn.commit()