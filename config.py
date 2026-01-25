from pathlib import Path

# CDP endpoint for your dedicated Chromium automation browser
CDP_URL = "http://127.0.0.1:9223"

# SQLite DB path
DB_PATH = Path("jobs.db")

# Logging folder for screenshots / HTML
ARTIFACT_DIR = Path("artifacts")
ARTIFACT_DIR.mkdir(exist_ok=True)

# Throttles (tune carefully to avoid Cloudflare)
SLEEP_BETWEEN_JOBS_SEC = 15
SLEEP_BETWEEN_STEPS_SEC = 2

# Safety
MAX_STEPS_PER_APPLICATION = 40
DEFAULT_TIMEOUT_MS = 60000