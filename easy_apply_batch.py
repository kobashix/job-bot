import subprocess
import sqlite3
import time
import sys

DB_PATH = "jobs.db"
PYTHON = sys.executable
RETRYABLE_STATUSES = (
    "applied",
    "external",
    "external_submitted",
    "blocked",
    "failed",
    "invalid",
    "no_apply",
    "permanent_failed",
    "captcha_pending",
    "non_remote",
    "unsupported_external_flow",
    "missing_required_fields",
    "navigation_blocked",
)

def fetch_jobs(limit):
    attempts = 0
    while True:
        try:
            with sqlite3.connect(DB_PATH, timeout=30) as conn:
                return conn.execute(
                    """
                    SELECT job_url
                    FROM jobs
                    WHERE applied = 0
                      AND (
                        lower(COALESCE(location, '')) LIKE '%remote%'
                        OR lower(COALESCE(title, '')) LIKE '%remote%'
                      )
                      AND (status IS NULL OR status NOT IN (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?))
                    LIMIT ?
                    """,
                    (*RETRYABLE_STATUSES, limit),
                ).fetchall()
        except sqlite3.OperationalError as exc:
            attempts += 1
            print(f"[DB] Fetch failed ({attempts}): {exc}")
            if "locked" in str(exc).lower() and attempts < 6:
                time.sleep(2 + attempts)
                continue
            raise

def mark(job_url, status, reason=None):
    attempts = 0
    while True:
        try:
            with sqlite3.connect(DB_PATH, timeout=30) as conn:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?, last_error = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE job_url = ?
                    """,
                    (status, reason, job_url),
                )
                conn.commit()
            return
        except sqlite3.OperationalError as exc:
            attempts += 1
            print(f"[DB] Update failed ({attempts}): {exc}")
            if "locked" in str(exc).lower() and attempts < 6:
                time.sleep(2 + attempts)
                continue
            raise

def run_and_stream(cmd):
    output = []
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as proc:
        for line in proc.stdout:
            print(line, end="")
            output.append(line)
        return proc.wait(), "".join(output)

def main(limit=10):
    jobs = fetch_jobs(limit)
    log = print

    log(f"Starting batch apply: {len(jobs)} jobs")

    success = 0
    fail = 0

    for idx, (url,) in enumerate(jobs, 1):
        log(f"\n[{idx}/{len(jobs)}] Applying to:\n{url}\n")

        returncode, output = run_and_stream([PYTHON, "easy_apply_single.py", url])

        if returncode == 0:
            success += 1
        else:
            fail += 1
            if "external" in output.lower():
                mark(url, "external", "company_site")
            elif "captcha" in output.lower():
                mark(url, "captcha_pending", "captcha_pending")
            elif "invalid" in output.lower() or "404" in output.lower():
                mark(url, "invalid", "invalid_job")
            else:
                fallback_reason = output[-500:] if output else f"unclassified_failure_returncode_{returncode}"
                mark(url, "failed", fallback_reason)

        time.sleep(8)

    log("\nBatch complete")
    log(f"Successes: {success}")
    log(f"Failures: {fail}")

if __name__ == "__main__":
    limit = int(sys.argv[sys.argv.index("--limit")+1]) if "--limit" in sys.argv else 10
    main(limit)
