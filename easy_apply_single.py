"""Backward-compatible entry point for single job application."""

from __future__ import annotations

import asyncio
import sqlite3

from helpers.utils import load_config, parse_args
from main import run as main_run

TERMINAL_STATUSES = (
    "APPLIED",
    "EXTERNAL_APPLY",
    "NOT_REMOTE",
    "EXPIRED",
    "CAPTCHA_BLOCKED",
)


def _ensure_terminal_status(db_path: str, job_url: str) -> None:
    with sqlite3.connect(db_path, timeout=30) as conn:
        row = conn.execute(
            "SELECT status, last_error FROM jobs WHERE job_url = ?",
            (job_url,),
        ).fetchone()
        if not row:
            return
        status, last_error = row
        if status and (status in TERMINAL_STATUSES or status.startswith("ERROR_")):
            return
        reason = last_error or "missing_terminal_status"
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, last_error = ?, applied = 0, updated_at = CURRENT_TIMESTAMP
            WHERE job_url = ?
            """,
            ("ERROR_missing_terminal_status", reason, job_url),
        )


async def run() -> int:
    args = parse_args()
    if not args.job_url:
        print("[FAIL] Missing job URL argument", flush=True)
        return 2
    config = load_config(args.config)
    code = await main_run()
    _ensure_terminal_status(str(config.db_path), args.job_url)
    return code


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
