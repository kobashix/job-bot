# Job Bot 2.0

A high-performance, asynchronous job application bot for Indeed and external ATS platforms.

## Core Features
- **Unified Async Architecture**: Built on Playwright and `asyncio` for speed and reliability.
- **Intelligent Form Filling**: Learns from previous answers and handles complex EEO/commute questions.
- **Robust Detection**: Advanced logic for detecting "Applied", "Expired", and "Non-Remote" jobs.
- **CAPTCHA Handling**: Integrated support for resolving challenges.

## Getting Started

### 1. Setup
- Install dependencies: `pip install -r requirements.txt`
- Install Playwright browsers: `playwright install chromium`
- Launch a browser with CDP enabled (default: `localhost:9223`).

### 2. Usage

#### Scrape Jobs
Scrape remote jobs from Indeed and save them to the database:
```bash
python scrape_jobs.py "https://www.indeed.com/jobs?q=software+engineer&l=remote"
```

#### Apply to Jobs
Apply to a single job URL:
```bash
python unified_apply.py "https://www.indeed.com/viewjob?jk=..."
```

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
