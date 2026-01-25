import sys
import time
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

JOB_URL = sys.argv[1]

DB_PATH = "jobs.db"
ANSWERS_FILE = Path("answers.json")

SUCCESS_TEXTS = [
    "application has been submitted",
    "thank you for applying",
]

BLOCK_TEXTS = [
    "i'm not a robot",
    "verify you are human",
    "captcha",
    "security check",
]

EXTERNAL_HOST_KEYWORDS = [
    "greenhouse",
    "lever.co",
    "workday",
    "paycom",
    "icims",
    "adp",
]

def log(msg):
    print(msg, flush=True)

def db_update(job_url, status, reason=None):
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.execute(
            """
            UPDATE jobs
            SET applied = ?, status = ?, last_error = ?, updated_at = CURRENT_TIMESTAMP
            WHERE job_url = ?
            """,
            (1 if status == "applied" else 0, status, reason, job_url),
        )
        conn.commit()

def load_answers():
    if not ANSWERS_FILE.exists():
        ANSWERS_FILE.write_text("{}")
    return json.loads(ANSWERS_FILE.read_text())

ANSWERS = load_answers()

def slow_wait(sec=2):
    time.sleep(sec)

def detect_captcha(page):
    body = page.inner_text("body").lower()
    for t in BLOCK_TEXTS:
        if t in body:
            log("[CAPTCHA] Detected blocking page")
            db_update(JOB_URL, "blocked", "captcha")
            sys.exit(20)

def detect_external(page):
    host = page.url.lower()
    if any(k in host for k in EXTERNAL_HOST_KEYWORDS):
        log("[EXTERNAL] Company site detected")
        db_update(JOB_URL, "external", "company_site")
        sys.exit(3)

def click_any(page, labels, timeout=5000):
    for label in labels:
        loc = page.locator(f"button:has-text('{label}')")
        if loc.count():
            try:
                loc.first.scroll_into_view_if_needed()
                loc.first.click(timeout=timeout)
                log(f"[CLICK] {label}")
                return True
            except Exception as e:
                log(f"[WARN] Click failed on {label}: {e}")
    return False

def handle_resume_screen(page):
    if page.locator("text=Add a resume for the employer").count():
        log("[RESUME] Selecting Indeed resume")
        card = page.locator("text=Use your Indeed Resume").first
        card.scroll_into_view_if_needed()
        card.click(force=True)
        slow_wait(2)
        click_any(page, ["Continue"], timeout=15000)

def handle_radios(page):
    fieldsets = page.locator("fieldset")
    for i in range(fieldsets.count()):
        fs = fieldsets.nth(i)
        radios = fs.locator("input[type='radio']")
        if radios.count():
            radios.first.check(force=True)
            log("[RADIO] Defaulted first option")

def handle_special_radios(page):
    mapping = {
        "male": "Male",
        "not hispanic": "Not Hispanic",
        "white": "White",
        "protected veteran": "No",
        "disability": "No, I do not have a disability",
    }
    body = page.inner_text("body").lower()
    for key, val in mapping.items():
        if key in body:
            loc = page.locator(f"label:has-text('{val}')")
            if loc.count():
                loc.first.click(force=True)
                log(f"[EEO] Selected {val}")

def handle_inputs(page):
    inputs = page.locator("input[type='text'], textarea")
    for i in range(inputs.count()):
        el = inputs.nth(i)
        label = el.evaluate("e => e.closest('div')?.innerText || ''").lower()
        if "your name" in label:
            el.fill("Andrew Pennington")
        elif "today" in label:
            el.fill(datetime.now().strftime("%m/%d/%Y"))
        elif "how many years" in label:
            el.fill("15")

def find_apply_button(page):
    return click_any(page, ["Apply", "Apply now", "Apply on company site"], timeout=15000)

with sync_playwright() as p:
    log("[INIT] Connecting to Chromium")
    browser = p.chromium.connect_over_cdp("http://localhost:9223")
    ctx = browser.contexts[0]
    page = ctx.new_page()

    log("[OPEN] Navigating to job page")
    page.goto(JOB_URL, timeout=60000)
    slow_wait(3)

    detect_captcha(page)
    detect_external(page)

    if not find_apply_button(page):
        log("[FAIL] No Apply CTA found")
        db_update(JOB_URL, "no_apply", "missing_apply_button")
        sys.exit(10)

    for step in range(30):
        log(f"[STEP] {step+1}")
        slow_wait(3)

        detect_captcha(page)
        detect_external(page)

        body = page.inner_text("body").lower()
        if any(t in body for t in SUCCESS_TEXTS):
            log("[SUCCESS] Application submitted")
            db_update(JOB_URL, "applied")
            sys.exit(0)

        handle_resume_screen(page)
        handle_inputs(page)
        handle_radios(page)
        handle_special_radios(page)

        if click_any(page, ["Continue", "Review", "Submit", "Submit your application"], timeout=20000):
            continue

        log("[WAIT] No actionable button – waiting")
        slow_wait(6)

    log("[FAIL] Max steps reached")
    db_update(JOB_URL, "failed", "max_steps")
    sys.exit(99)
