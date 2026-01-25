import subprocess
import sqlite3
import time
import sys

DB_PATH = "jobs.db"
PYTHON = sys.executable

def fetch_jobs(limit):
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        return conn.execute(
            """
            SELECT job_url
            FROM jobs
            WHERE applied = 0
              AND status IS NULL
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

def mark(job_url, status, reason=None):
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

def main(limit=10):
    jobs = fetch_jobs(limit)
    log = print

    log(f"Starting batch apply: {len(jobs)} jobs")

    success = 0
    fail = 0

    for idx, (url,) in enumerate(jobs, 1):
        log(f"\n[{idx}/{len(jobs)}] Applying to:\n{url}\n")

        result = subprocess.run(
            [PYTHON, "easy_apply_single.py", url],
            capture_output=True,
            text=True,
        )

        output = result.stdout + result.stderr
        print(output)

        if result.returncode == 0:
            success += 1
        else:
            fail += 1
            if "external" in output.lower():
                mark(url, "external", "company_site")
            elif "captcha" in output.lower():
                mark(url, "blocked", "captcha")
            else:
                mark(url, "failed", output[-500:])

        time.sleep(8)

    log("\nBatch complete")
    log(f"Successes: {success}")
    log(f"Failures: {fail}")

if __name__ == "__main__":
    limit = int(sys.argv[sys.argv.index("--limit")+1]) if "--limit" in sys.argv else 10
    main(limit)
