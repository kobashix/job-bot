# Job Bot 2.0 - AI Coding Agent Instructions

## Project Overview
**Job-bot** automates Indeed Easy Apply job applications using Playwright CDP (Chrome DevTools Protocol) connected to a dedicated browser instance. It handles form filling, multi-step application flows, external ATS redirects, captcha detection, and maintains centralized SQLite state.

## Architecture Essentials

### Core Components
- **`core/session.py` (849 lines)**: Main orchestrator (`PlaywrightSession`). Manages page navigation, detects invalid/expired jobs, applied badges, CAPTCHA blocks, and routes between Indeed internal forms and external ATS flows. Core loop iterates up to `MAX_STEPS_PER_APPLICATION` (40).
- **`helpers/form_fill.py`**: Auto-fills inputs (text, selects, radio buttons). Handles resume selection screens, voluntary EEO demographics, and learns form answers into `answers.json` for reuse.
- **`helpers/db.py`**: Async SQLite client with lock-based retry handling for job status (`applied`, `external`, `captcha_pending`, `failed`, `invalid`). Thread-safe write operations.
- **`helpers/captcha.py`**: Detects reCAPTCHA/hCaptcha iframes and text ("i'm not a robot", "verify you are human"). Pauses for manual resolution (120s default).
- **`db.py`**: Legacy schema initialization (jobs, answers, runs, job_events tables).

### Critical Data Flow
1. **Entry** (`main.py`): Parse CLI args (`job_url`, `--train`, `--config`), load profile from `config.json`, initialize DB + captcha handler.
2. **Session spawn** (`core/session.py`): Connect CDP to browser at `config.cdp_url` (default `http://127.0.0.1:9223`).
3. **Validation loop**: Check HTTP status, expired banners, applied badge. Update DB status; exit early on invalid.
4. **Apply loop**: Auto-fill fields, click Continue/Review/Submit. Detect external ATS host change; branch accordingly.
5. **External flow**: Fill typical fields (name, email, phone, location) from `profile`. Attempt submit (skip in `--train`).
6. **Completion**: Update DB with `status` + `reason`. Save artifacts (screenshots, HTML) on failure.

## Key Workflows & Commands

### Local Development
```powershell
# Activate venv
.\.venv\Scripts\Activate.ps1

# Launch Edge with remote debugging (separate profile, isolated from your main browser)
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" `
  --remote-debugging-port=9223 `
  --user-data-dir="C:\temp\job-bot-edge-profile"

# Single application with training mode (learns form inputs into answers.json)
python .\easy_apply_single.py --train "https://www.indeed.com/viewjob?jk=JOB_ID"

# Batch process jobs where applied=0 and status is non-terminal
python .\easy_apply_batch.py --limit 10
```

### Critical Patterns & Conventions

1. **Database Status Taxonomy** (See `CODEX_CONTRACT.md`):
   - `applied`: Successfully submitted (internal or external)
   - `external`: Detected external ATS; will retry
   - `external_pending`: Submitted to external ATS
   - `captcha_pending`: Blocked by captcha; manual resolution needed
   - `invalid` (expired): Job expired or 404/410; no retry
   - `failed`: Exception caught; update `last_error` reason (not generic "failed")

2. **External ATS Detection** (`core/session.py`):
   - Watch for host change: `indeed.com` → `greenhouse.io`, `lever.co`, etc.
   - List in `EXTERNAL_HOST_KEYWORDS`: greenhouse, lever, workday, paycom, icims, adp, taleo, etc.
   - Button labels: "Apply on company site", "Apply externally"
   - Route: Fill external form fields; detect final submit; save failure screenshots in `artifacts/`.

3. **Applied Badge Detection** (Multiple fallbacks):
   - Role-based: `page.get_by_role("button", name="Applied")`
   - Selectors: `button[aria-label='Applied']`, `span/div:has-text('Applied')`
   - Text fallback: `"applied"` in page text
   - **Action**: Immediately update DB to `applied`, exit session.

4. **Form Filling Defaults** (`FormFiller`):
   - Resume screen: Select "Use your Indeed Resume" (not upload)
   - EEO voluntary fields:
     - Gender: Male
     - Ethnicity: Not Hispanic or Latino
     - Race: White
     - Veteran: No
     - Disability: No, I do not have a disability
   - "How many years" → 15 (hard-coded in profile/form filler)
   - First radio option selected if unanswered
   - Learned answers persist in `answers.json`; reuse on similar questions

5. **Retry & Wait Strategy**:
   - No actionable button found → retry up to 8 seconds with exponential backoff (`CLICK_RETRY_BASE_SECONDS = 0.6`)
   - DB lock → sleep 1+attempts seconds, max 6 retries
   - Captcha detected → pause session, wait for manual resolution (120s)
   - Default timeout: 60 seconds (configurable)

6. **Training Mode** (`--train` flag):
   - Still navigates and fills forms
   - Skips final submit clicks on external ATS
   - Saves screenshots on external failures for manual review
   - Records form answers into `answers.json` for future reuse

## Important File Locations
- **Config**: `config.json` (profile name, email, phone; CDP URL; DB/artifacts paths)
- **Database**: `jobs.db` (SQLite; schema in `db.py`)
- **Answers**: `answers.json` (learned form fields)
- **Artifacts**: `artifacts/` (screenshots, HTML on failure)
- **Logs**: `easy_apply_batch.txt` (batch runner output)

## Non-Negotiable Behavior (from CODEX_CONTRACT)
- **Never remove** existing functionality
- **Never reduce waits** unless explicitly instructed
- **Every failure must categorized** with explicit reason (not generic "failed")
- **Always default-select first radio** if no answer provided
- **Never requeue** external/blocked/dead jobs
- **Final submit** must wait explicitly for button visibility
- **No pseudocode**; code must be runnable without environment mods

## Common Debugging Points
- **CDP connection fails**: Check `config.cdp_url` and browser remote-debug port match
- **DB locked**: Multiple processes accessing DB; increase retry timeout or serialize runs
- **Captcha stalls**: Manual resolution window may have closed; logs should show wait timestamp
- **Form mismatch**: Field not found; check `AnswersStore` aliases and locator cache in `FormFiller`
- **External ATS not detected**: Verify host is in `EXTERNAL_HOST_KEYWORDS` or button label matches `EXTERNAL_BUTTON_LABELS`
