"""Batch runner for easy apply using the async single-apply entry point."""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from dataclasses import dataclass
from typing import Iterable, Optional

from helpers.db import DBClient
from helpers.utils import ContextLogger, load_config, setup_logging


TERMINAL_STATUSES = (
    "APPLIED",
    "EXTERNAL_APPLY",
    "NOT_REMOTE",
    "EXPIRED",
    "CAPTCHA_BLOCKED",
)


@dataclass
class BatchResult:
    success: int = 0
    failed: int = 0


class BatchRunner:
    """Runs easy_apply_single.py for a batch of jobs."""

    def __init__(self, db: DBClient, logger, python_exe: str) -> None:
        self.db = db
        self.logger = logger
        self.python_exe = python_exe
        self.status_counts: dict[str, int] = {}

    async def run(self, limit: int) -> BatchResult:
        jobs = await self.db.fetch_jobs(limit, TERMINAL_STATUSES)
        self.logger.info("Starting batch apply: %s jobs", len(jobs))
        result = BatchResult()

        for idx, (url,) in enumerate(jobs, 1):
            self.logger.info("[%s/%s] Applying to: %s", idx, len(jobs), url)
            returncode, output = await self._run_single(url)
            status = await self._fetch_status(url)
            if returncode == 0:
                result.success += 1
            else:
                result.failed += 1
                await self._mark_failure(url, returncode, output, status)
                status = await self._fetch_status(url)
            if status:
                self.status_counts[status] = self.status_counts.get(status, 0) + 1
            await asyncio.sleep(3)

        self.logger.info("Batch complete")
        self.logger.info("Successes: %s", result.success)
        self.logger.info("Failures: %s", result.failed)
        if self.status_counts:
            for status, count in sorted(self.status_counts.items()):
                self.logger.info("Status %s: %s", status, count)
        if result.success == 0 and result.failed == 0:
            self.logger.warning(
                "No jobs processed. Check filters, job status, or database contents."
            )
        return result

    async def _run_single(self, url: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            self.python_exe,
            "easy_apply_single.py",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output_lines: list[str] = []
        assert proc.stdout is not None
        async for line in proc.stdout:
            decoded = line.decode(errors="ignore")
            print(decoded, end="")
            output_lines.append(decoded)
        returncode = await proc.wait()
        return returncode, "".join(output_lines)

    async def _mark_failure(
        self, url: str, returncode: int, output: str, current_status: Optional[str]
    ) -> None:
        if current_status and self._is_terminal(current_status):
            return
        lowered = output.lower() if output else ""
        if "external" in lowered:
            await self.db.update_job(url, "EXTERNAL_APPLY", "company_site", is_external=True)
            return
        if "captcha" in lowered:
            await self.db.update_job(url, "CAPTCHA_BLOCKED", "captcha_detected")
            return
        if "expired" in lowered or "not found" in lowered:
            await self.db.update_job(url, "EXPIRED", "expired_batch")
            return
        if "not remote" in lowered or "non_remote" in lowered:
            await self.db.update_job(url, "NOT_REMOTE", "not_remote_batch")
            return
        reason = output[-500:] if output else f"returncode_{returncode}"
        await self.db.update_job(url, f"ERROR_returncode_{returncode}", reason)

    async def _fetch_status(self, url: str) -> Optional[str]:
        def _fetch_sync() -> Optional[str]:
            with sqlite3.connect(self.db.db_path, timeout=30) as conn:
                row = conn.execute(
                    "SELECT status FROM jobs WHERE job_url = ?",
                    (url,),
                ).fetchone()
                return row[0] if row else None

        return await asyncio.to_thread(_fetch_sync)

    def _is_terminal(self, status: str) -> bool:
        return status in TERMINAL_STATUSES or status.startswith("ERROR_")


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch runner for easy apply")
    parser.add_argument("--limit", type=int, default=10, help="Max jobs to process")
    parser.add_argument("--config", default="config.json", help="Path to config JSON")
    return parser.parse_args(list(argv))


async def run() -> int:
    args = parse_args(sys.argv[1:])
    config = load_config(args.config)
    logger = setup_logging()
    context_logger = ContextLogger(logger, {"job_url": "batch", "ats": "batch", "step": "batch"})
    db_client = DBClient(str(config.db_path), context_logger)
    runner = BatchRunner(db_client, context_logger, sys.executable)
    await runner.run(args.limit)
    return 0


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
