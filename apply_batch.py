import sys
import time
import subprocess
from config import SLEEP_BETWEEN_JOBS_SEC
from db import start_run, end_run, fetch_jobs_for_apply, bump_attempt, set_job_status, log_event

def main():
    limit = 25
    if "--limit" in sys.argv:
        i = sys.argv.index("--limit")
        limit = int(sys.argv[i+1])

    run_id = start_run("apply_batch", notes=f"limit={limit}")
    try:
        jobs = list(fetch_jobs_for_apply(limit=limit))
        print(f"RUN: applying up to {len(jobs)} jobs")

        for row in jobs:
            job_id = row["job_id"]
            url = row["job_url"]
            bump_attempt(job_id)

            print(f"\nJOB {job_id} -> {url}")
            log_event(run_id, "INFO", "APPLY_START", job_id=job_id, url=url)

            try:
                # Use venv python explicitly
                proc = subprocess.run(
                    [r"venv\Scripts\python.exe", "apply_one.py", url],
                    capture_output=True,
                    text=True,
                    timeout=900
                )
                out = (proc.stdout or "") + (proc.stderr or "")
                tail = "\n".join(out.splitlines()[-20:]).strip()
                print(tail)

                if "SUBMITTED" in out:
                    set_job_status(job_id, "applied", applied=True)
                    log_event(run_id, "INFO", "APPLIED", job_id=job_id, url=url)
                elif "ALREADY_APPLIED" in out:
                    set_job_status(job_id, "applied", applied=True)
                    log_event(run_id, "INFO", "ALREADY_APPLIED", job_id=job_id, url=url)
                elif "EXTERNAL_ATS" in out or "EXTERNAL_SITE" in out:
                    set_job_status(job_id, "skipped_external", is_external=True)
                    log_event(run_id, "INFO", "SKIPPED_EXTERNAL", job_id=job_id, url=url)
                elif "PAUSE:" in out or "CLOUDFLARE" in out:
                    set_job_status(job_id, "captcha_blocked", last_error=tail)
                    log_event(run_id, "WARN", "CAPTCHA_BLOCKED", job_id=job_id, url=url)
                    # Stop batch on cloudflare to prevent poisoning session
                    print("STOP: verification detected. Solve it and re-run batch.")
                    break
                elif "QUESTIONNAIRE" in out:
                    set_job_status(job_id, "manual_required", last_error=tail)
                    log_event(run_id, "INFO", "MANUAL_REQUIRED", job_id=job_id, url=url)
                elif "STALLED" in out:
                    set_job_status(job_id, "failed_known", last_error=tail)
                    log_event(run_id, "ERROR", "STALLED", job_id=job_id, url=url)
                else:
                    set_job_status(job_id, "failed_known", last_error=tail)
                    log_event(run_id, "ERROR", "FAILED_KNOWN", job_id=job_id, url=url,)

            except subprocess.TimeoutExpired:
                set_job_status(job_id, "failed_known", last_error="TIMEOUT")
                log_event(run_id, "ERROR", "TIMEOUT", job_id=job_id, url=url)

            time.sleep(SLEEP_BETWEEN_JOBS_SEC)

    finally:
        end_run(run_id)

if __name__ == "__main__":
    main()
