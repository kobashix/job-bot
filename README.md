# Job Bot 2.0

A high-performance, asynchronous job application bot for Indeed and external ATS platforms.

## Core Features
- **Unified Async Architecture**: Built on Playwright and `asyncio` for speed and reliability.
- **Intelligent Form Filling**: Learns from previous answers and handles complex EEO/commute questions.
- **Robust Detection**: Advanced logic for detecting "Applied", "Expired", and "Non-Remote" jobs.
- **CAPTCHA Handling**: Integrated support for resolving challenges.

## Windows 11 / PowerShell Setup

To install and set up everything (or fix a broken environment), run this command:
```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force; .\setup.ps1
```

### Running the Bot
> [!IMPORTANT]
> You **must** have a browser running with remote debugging enabled for the bot to work.

1. **Launch Browser**: Run `.\launch_browser.ps1`. 
   - This will open a new Edge or Chrome window.
   - Keep this window open while the bot runs.
2. **Apply Jobs**:
   - **Apply (Single)**: `.\.venv\Scripts\python.exe unified_apply.py "JOB_URL"`
   - **Apply (Batch)**: `.\.venv\Scripts\python.exe unified_apply.py`
   - **Scrape**: `.\.venv\Scripts\python.exe scrape_jobs.py "SEARCH_URL"`

Run in **batch mode** (processes the next 10 jobs in the database):
```bash
python unified_apply.py
```

#### Training Mode
Run in training mode to learn new form answers:
```bash
python unified_apply.py --train "JOB_URL"
```

## Configuration
Update `config.json` with your profile details and preferences.

## Advanced
- **APPLY_LIMIT**: Environment variable to set the number of jobs to process in batch mode (default: 10).
- **Database**: All job statuses and application history are stored in `jobs.db`.
