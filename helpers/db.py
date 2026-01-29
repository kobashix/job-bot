"""Database utilities with retry handling."""
from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from typing import Optional


@dataclass
class DBResult:
    """Represents database operation outcome."""

    success: bool
    attempts: int


class DBClient:
    """Thread-safe SQLite client with retries."""

    def __init__(self, db_path: str, logger) -> None:
        self.db_path = db_path
        self.logger = logger
        self._lock = asyncio.Lock()

    async def _execute(self, query: str, params: tuple) -> DBResult:
        attempts = 0
        while True:
            attempts += 1
            try:
                async with self._lock:
                    await asyncio.to_thread(self._execute_sync, query, params)
                return DBResult(True, attempts)
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower() and attempts < 6:
                    self.logger.warning("DB locked; retrying", extra={"attempts": attempts})
                    await asyncio.sleep(1 + attempts)
                    continue
                raise

    def _execute_sync(self, query: str, params: tuple) -> None:
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.execute(query, params)
            conn.commit()

    async def fetch_jobs(self, limit: int, excluded_statuses: tuple[str, ...]) -> list[tuple[str]]:
        """Fetch a batch of job URLs with retry handling."""

        attempts = 0
        query = """
            SELECT job_url
            FROM jobs
            WHERE applied = 0
              AND (status IS NULL OR status NOT IN ({placeholders}))
            LIMIT ?
        """.format(placeholders=", ".join("?" for _ in excluded_statuses))
        params = (*excluded_statuses, limit)
        while True:
            attempts += 1
            try:
                async with self._lock:
                    return await asyncio.to_thread(self._fetch_sync, query, params)
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower() and attempts < 6:
                    self.logger.warning("DB locked; retrying fetch", extra={"attempts": attempts})
                    await asyncio.sleep(1 + attempts)
                    continue
                raise

    def _fetch_sync(self, query: str, params: tuple) -> list[tuple[str]]:
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            return conn.execute(query, params).fetchall()

    async def update_job(
        self,
        job_url: str,
        status: str,
        reason: Optional[str] = None,
        is_external: Optional[bool] = None,
    ) -> DBResult:
        """Update job status and metadata."""

        fields = [
            "applied = ?",
            "status = ?",
            "last_error = ?",
            "updated_at = CURRENT_TIMESTAMP",
        ]
        values = [1 if status == "applied" else 0, status, reason]
        if is_external is not None:
            fields.append("is_external = ?")
            values.append(1 if is_external else 0)
        values.append(job_url)
        query = f"UPDATE jobs SET {', '.join(fields)} WHERE job_url = ?"
        return await self._execute(query, tuple(values))

    async def update_job_metadata(
        self,
        job_url: str,
        title: Optional[str] = None,
        company: Optional[str] = None,
        location: Optional[str] = None,
    ) -> DBResult:
        """Update job metadata fields when available."""

        fields = []
        values = []
        if title:
            fields.append("title = ?")
            values.append(title)
        if company:
            fields.append("company = ?")
            values.append(company)
        if location:
            fields.append("location = ?")
            values.append(location)
        if not fields:
            return DBResult(True, 0)
        fields.append("updated_at = CURRENT_TIMESTAMP")
        values.append(job_url)
        query = f"UPDATE jobs SET {', '.join(fields)} WHERE job_url = ?"
        return await self._execute(query, tuple(values))

    async def delete_job(self, job_url: str) -> DBResult:
        """Delete job from the database."""

        query = "DELETE FROM jobs WHERE job_url = ?"
        result = await self._execute(query, (job_url,))
        self.logger.info("Deleted job", extra={"deleted_url": job_url})
        return result
