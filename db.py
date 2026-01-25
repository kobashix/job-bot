import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Optional, Dict, Any, Iterable, Tuple
from config import DB_PATH

def utcnow() -> str:
    return datetime.utcnow().isoformat()

@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()

def init_schema():
    with conn() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            job_url TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'indeed',
            title TEXT,
            company TEXT,
            location TEXT,
            is_external INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'new',
            applied INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS answers (
            key TEXT PRIMARY KEY,
            default_value TEXT NOT NULL,
            aliases_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            notes TEXT
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS job_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT,
            run_id INTEGER,
            ts TEXT NOT NULL,
            level TEXT NOT NULL,
            message TEXT NOT NULL,
            url TEXT,
            artifact_prefix TEXT,
            FOREIGN KEY(job_id) REFERENCES jobs(job_id),
            FOREIGN KEY(run_id) REFERENCES runs(run_id)
        )
        """)

def start_run(kind: str, notes: str = "") -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO runs(kind, started_at, notes) VALUES (?, ?, ?)",
            (kind, utcnow(), notes)
        )
        return int(cur.lastrowid)

def end_run(run_id: int):
    with conn() as c:
        c.execute("UPDATE runs SET ended_at=? WHERE run_id=?", (utcnow(), run_id))

def upsert_job(job_id: str, job_url: str, title: Optional[str] = None,
               company: Optional[str] = None, location: Optional[str] = None,
               source: str = "indeed"):
    now = utcnow()
    with conn() as c:
        existing = c.execute("SELECT job_id FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if existing:
            c.execute("""
                UPDATE jobs SET
                    job_url=?,
                    title=COALESCE(?, title),
                    company=COALESCE(?, company),
                    location=COALESCE(?, location),
                    updated_at=?
                WHERE job_id=?
            """, (job_url, title, company, location, now, job_id))
        else:
            c.execute("""
                INSERT INTO jobs(job_id, job_url, source, title, company, location, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (job_id, job_url, source, title, company, location, now, now))

def set_job_status(job_id: str, status: str, applied: Optional[bool] = None,
                   is_external: Optional[bool] = None, last_error: Optional[str] = None):
    now = utcnow()
    with conn() as c:
        parts = ["status=?", "updated_at=?"]
        vals = [status, now]
        if applied is not None:
            parts.append("applied=?")
            vals.append(1 if applied else 0)
        if is_external is not None:
            parts.append("is_external=?")
            vals.append(1 if is_external else 0)
        if last_error is not None:
            parts.append("last_error=?")
            vals.append(last_error[:2000])
        vals.append(job_id)
        c.execute(f"UPDATE jobs SET {', '.join(parts)} WHERE job_id=?", vals)

def bump_attempt(job_id: str, error: Optional[str] = None):
    with conn() as c:
        c.execute("""
            UPDATE jobs
            SET attempts = attempts + 1,
                last_attempt_at = ?,
                last_error = ?
            WHERE job_id = ?
        """, (utcnow(), (error or "")[:2000], job_id))

def fetch_jobs_for_apply(limit: int = 25) -> Iterable[sqlite3.Row]:
    with conn() as c:
        rows = c.execute("""
            SELECT * FROM jobs
            WHERE applied=0
              AND status IN ('new','retry')
            ORDER BY updated_at ASC
            LIMIT ?
        """, (limit,)).fetchall()
        return rows

def log_event(run_id: int, level: str, message: str, job_id: Optional[str] = None,
              url: Optional[str] = None, artifact_prefix: Optional[str] = None):
    with conn() as c:
        c.execute("""
            INSERT INTO job_events(job_id, run_id, ts, level, message, url, artifact_prefix)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (job_id, run_id, utcnow(), level, message[:2000], url, artifact_prefix))
