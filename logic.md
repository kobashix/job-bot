# Application Bot Logic

This document explains the end-to-end logic behind the job application bot, including the session flow, database updates, external ATS handling, and training mode behavior.

## High-Level Flow

1. **Entry point (`main.py`)**
   - Parse CLI arguments (`job_url`, `--train`, `--config`).
   - Load config (profile, DB path, answers file, artifact path).
   - Initialize logging, DB client, captcha handler, and form filler.
   - Start a `PlaywrightSession` for the job URL.

2. **Session startup (`core/session.py`)**
   - Connect to Chromium via CDP (`config.cdp_url`) and open a new page.
   - Navigate to the job URL.
   - Perform early detection and metadata capture.

3. **Landing checks**
   - **Expired/invalid** checks: detect status codes and expired banners. Update DB and exit if invalid/expired. 404/410 deletes the job.
   - **Metadata capture**: update `title`, `company`, and `location` in the DB when present.
   - **Already applied** detection: short-circuit if the page shows the `Applied` badge.
   - **CAPTCHA** detection: pause for resolution if needed.
   - **Apply CTA**: try `Apply`/`Apply now` buttons and link CTAs.

4. **Apply loop**
   - Iterate up to `config.max_steps` while on internal Indeed flows.
   - Each step re-checks invalid/expired, metadata, non-remote, applied badge, and CAPTCHA.
   - Auto-fill inputs and selection controls.
   - Click `Continue`, `Review`, or `Submit` until completion.
   - Detect success (`Application submitted` / `Thank you` text) and update DB.

5. **External ATS flow**
   - If the host changes or external CTA is detected, branch to external flow.
   - Fill typical fields and demographics using profile values.
   - Click external `Next/Continue` buttons.
   - Attempt final submit (skipped in `--train` mode).
   - On success, update DB. On failure, save a screenshot and update DB with a failure reason.

## Database Status Updates

The bot uses a centralized DB helper (`helpers/db.py`) for status writes:

- **Applied**: `status=applied`, `applied=1`
- **Already applied**: `status=applied`, reason `already_applied`
- **Expired**: `status=invalid` with reason `expired_on_indeed`
- **404/410**: delete job from DB
- **External**: `status=external` and `is_external=1`
- **Captcha**: `status=captcha_pending`
- **Failures**: `status=failed` with reason

## Applied Badge Detection

When a job page shows an Applied badge, the bot exits early to avoid re-applying:

- Role-based detection: `page.get_by_role("button", name="Applied")`
- Selectors: `button[aria-label='Applied']`, `span/div:has-text('Applied')`, `data-testid` and `aria-label` applied attributes
- Page text fallback containing `applied`

The status is immediately updated to `applied` in the DB and the session exits.

## Expired Job Detection

Expired jobs are detected via:

- HTTP response status codes (404/410 are deleted immediately)
- Page banners such as `This job has expired on Indeed` (marked invalid)
- Generic invalid text (e.g., `job expired`, `page not found`)

## Training Mode (`--train`)

- Skips final submit clicks on external ATS.
- Still performs navigation, form filling, and field detection.
- Saves screenshots on external failures for review.

## Batch Runner

The batch runner (`easy_apply_batch.py`):

- Fetches jobs where `applied=0` and status is not terminal.
- Runs `easy_apply_single.py` per job as a subprocess.
- Streams logs to console and updates DB based on output classification.

## Configuration and Answers

- Profile values (name, email, EEO defaults) are loaded from `config.json` and environment overrides.
- `answers.json` is updated in training mode to remember new form inputs for later reuse.

---

This logic is designed to be resilient to dynamic pages, avoid duplicate applications, and maintain accurate job status in the database.
