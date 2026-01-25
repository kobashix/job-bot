import sys
import time
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

JOB_URL = sys.argv[1]
JOB_HOST = urlparse(JOB_URL).netloc.lower()

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
    "taleo",
    "brassring",
    "successfactors",
]

def log(msg):
    print(msg, flush=True)

def db_update(job_url, status, reason=None):
    attempts = 0
    while True:
        try:
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
            return
        except sqlite3.OperationalError as exc:
            attempts += 1
            log(f"[DB] Update failed ({attempts}): {exc}")
            if "locked" in str(exc).lower() and attempts < 6:
                time.sleep(2 + attempts)
                continue
            raise

def load_answers():
    if not ANSWERS_FILE.exists():
        ANSWERS_FILE.write_text("{}")
    return json.loads(ANSWERS_FILE.read_text())

ANSWERS = load_answers()

def slow_wait(sec=2):
    time.sleep(sec)

def detect_captcha(page):
    body = ""
    title = ""
    try:
        body = page.inner_text("body").lower()
        title = page.title().lower()
    except Exception as exc:
        log(f"[WARN] Failed reading page text: {exc}")
    for t in BLOCK_TEXTS:
        if t in body or t in title:
            log(f"[CAPTCHA] Detected blocking page: {t}")
            db_update(JOB_URL, "blocked", "captcha")
            sys.exit(20)
    captcha_frames = page.locator("iframe[src*='captcha' i], iframe[title*='captcha' i], div[aria-label*='captcha' i]")
    if captcha_frames.count():
        log("[CAPTCHA] Detected captcha iframe")
        db_update(JOB_URL, "blocked", "captcha")
        sys.exit(20)

def detect_external(page):
    current_url = page.url
    host = urlparse(current_url).netloc.lower()
    if host and host != JOB_HOST and "indeed.com" not in host:
        log(f"[EXTERNAL] Host changed: {host}")
        db_update(JOB_URL, "external", f"host_change:{host}")
        sys.exit(3)
    if any(k in host for k in EXTERNAL_HOST_KEYWORDS):
        log(f"[EXTERNAL] Company site detected: {host}")
        db_update(JOB_URL, "external", f"company_site:{host}")
        sys.exit(3)

def detect_invalid(page, response=None):
    status = None
    if response is not None:
        try:
            status = response.status
        except Exception:
            status = None
    if status and status >= 400:
        log(f"[INVALID] HTTP status {status}")
        db_update(JOB_URL, "invalid", f"http_status_{status}")
        sys.exit(12)
    try:
        body = page.inner_text("body").lower()
        title = page.title().lower()
    except Exception as exc:
        log(f"[WARN] Failed reading page text: {exc}")
        return
    invalid_texts = [
        "job expired",
        "job is no longer available",
        "job has expired",
        "this job is no longer available",
        "404",
        "page not found",
    ]
    for text in invalid_texts:
        if text in body or text in title:
            log(f"[INVALID] {text}")
            db_update(JOB_URL, "invalid", f"invalid_text:{text}")
            sys.exit(12)

def click_any(page, labels, timeout=5000):
    for label in labels:
        loc = page.locator(f"button:has-text('{label}')")
        if loc.count():
            try:
                loc.first.scroll_into_view_if_needed()
                loc.first.click(timeout=timeout, force=True)
                log(f"[CLICK] {label}")
                return True
            except Exception as e:
                log(f"[WARN] Click failed on {label}: {e}")
                try:
                    loc.first.scroll_into_view_if_needed()
                    loc.first.click(timeout=timeout, force=True)
                    log(f"[CLICK] Retry success: {label}")
                    return True
                except Exception as retry_exc:
                    log(f"[WARN] Retry click failed on {label}: {retry_exc}")
    return False

def handle_resume_screen(page):
    if page.locator("text=Add a resume for the employer").count() or page.locator("text=Use your Indeed Resume").count():
        log("[RESUME] Selecting Indeed resume")
        card = page.locator("text=Use your Indeed Resume").first
        try:
            card.scroll_into_view_if_needed()
            card.click(force=True)
        except Exception as exc:
            log(f"[WARN] Resume card click failed: {exc}")
        slow_wait(2)
        click_any(page, ["Continue"], timeout=20000)

def handle_radios(page):
    fieldsets = page.locator("fieldset")
    for i in range(fieldsets.count()):
        fs = fieldsets.nth(i)
        radios = fs.locator("input[type='radio']")
        if radios.count():
            try:
                radios.first.scroll_into_view_if_needed()
                radios.first.check(force=True)
                log("[RADIO] Defaulted first option")
            except Exception as exc:
                log(f"[WARN] Radio default failed: {exc}")

def handle_special_radios(page):
    selections = [
        ("gender", "Male"),
        ("sex", "Male"),
        ("ethnicity", "Not Hispanic or Latino"),
        ("hispanic", "Not Hispanic or Latino"),
        ("race", "White"),
        ("veteran", "No"),
        ("disability", "No, I do not have a disability and have not had one in the past"),
    ]
    try:
        body = page.inner_text("body").lower()
    except Exception as exc:
        log(f"[WARN] Failed reading page text: {exc}")
        body = ""
    for key, val in selections:
        if key in body:
            loc = page.locator(f"label:has-text('{val}')")
            if loc.count():
                try:
                    loc.first.scroll_into_view_if_needed()
                    loc.first.click(force=True)
                    log(f"[EEO] Selected {val}")
                except Exception as exc:
                    log(f"[WARN] EEO selection failed for {val}: {exc}")

def extract_context(el):
    try:
        label = el.evaluate(
            """e => {
                const label = e.closest('label') || document.querySelector(`label[for='${e.id}']`);
                const aria = e.getAttribute('aria-label') || e.getAttribute('placeholder') || '';
                const parentText = e.closest('div')?.innerText || '';
                return [label?.innerText || '', aria, parentText].join(' ').toLowerCase();
            }"""
        )
    except Exception:
        label = ""
    return label

def handle_inputs(page):
    inputs = page.locator("input[type='text'], textarea")
    for i in range(inputs.count()):
        el = inputs.nth(i)
        context = extract_context(el)
        try:
            if "your name" in context or "full name" in context or "name" in context:
                el.scroll_into_view_if_needed()
                el.fill("Andrew Pennington")
                log("[INPUT] Filled name")
            elif "today" in context or "date" in context:
                el.scroll_into_view_if_needed()
                el.fill(datetime.now().strftime("%m/%d/%Y"))
                log("[INPUT] Filled date")
            elif "how many years" in context or "years of" in context:
                el.scroll_into_view_if_needed()
                el.fill("15")
                log("[INPUT] Filled years")
        except Exception as exc:
            log(f"[WARN] Input fill failed: {exc}")

def find_apply_button(page):
    return click_any(page, ["Apply", "Apply now", "Apply on company site"], timeout=15000)

try:
    with sync_playwright() as p:
        log("[INIT] Connecting to Chromium")
        browser = p.chromium.connect_over_cdp("http://localhost:9223")
        ctx = browser.contexts[0]
        page = ctx.new_page()

        log("[OPEN] Navigating to job page")
        response = None
        try:
            response = page.goto(JOB_URL, timeout=60000)
        except PWTimeout as exc:
            log(f"[FAIL] Navigation timeout: {exc}")
            db_update(JOB_URL, "failed", "navigation_timeout")
            sys.exit(11)
        slow_wait(4)

        detect_invalid(page, response)
        detect_captcha(page)
        detect_external(page)

        if not find_apply_button(page):
            log("[FAIL] No Apply CTA found")
            db_update(JOB_URL, "no_apply", "missing_apply_button")
            sys.exit(10)

        for step in range(30):
            log(f"[STEP] {step+1}")
            slow_wait(4)

            detect_invalid(page)
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
                slow_wait(3)
                continue

            log("[WAIT] No actionable button – waiting")
            slow_wait(6)

        log("[FAIL] Max steps reached")
        db_update(JOB_URL, "failed", "max_steps")
        sys.exit(99)
except SystemExit:
    raise
except Exception as exc:
    log(f"[FAIL] Unhandled exception: {exc}")
    db_update(JOB_URL, "failed", f"unhandled_exception:{exc}")
    sys.exit(98)
