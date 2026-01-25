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
CAPTCHA_IFRAME_SELECTORS = [
    "iframe[src*='captcha' i]",
    "iframe[src*='recaptcha' i]",
    "iframe[src*='hcaptcha' i]",
    "iframe[title*='captcha' i]",
    "iframe[title*='recaptcha' i]",
    "iframe[title*='hcaptcha' i]",
    "div[aria-label*='captcha' i]",
]

LAST_ACTION = {"label": None, "fn": None}
LAST_PROGRESS = {"url": None, "value": None, "timestamp": None}

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

def mark_captcha_pending(reason):
    log(f"[CAPTCHA] {reason}")
    db_update(JOB_URL, "captcha_pending", reason)

def prompt_captcha_and_retry():
    log("[CAPTCHA] Solve captcha in browser, then press ENTER to continue")
    try:
        input()
    except EOFError:
        log("[CAPTCHA] No stdin available to continue")
    if LAST_ACTION["fn"] is not None:
        log(f"[CAPTCHA] Retrying last action: {LAST_ACTION['label']}")
        try:
            LAST_ACTION["fn"]()
        except Exception as exc:
            log(f"[WARN] Retry action failed after captcha: {exc}")

def detect_captcha(page, reason="captcha_detected"):
    body = ""
    title = ""
    try:
        body = page.inner_text("body").lower()
        title = page.title().lower()
    except Exception as exc:
        log(f"[WARN] Failed reading page text: {exc}")
    for t in BLOCK_TEXTS:
        if t in body or t in title:
            mark_captcha_pending(f"{reason}:{t}")
            prompt_captcha_and_retry()
            return True
    captcha_frames = page.locator(", ".join(CAPTCHA_IFRAME_SELECTORS))
    if captcha_frames.count():
        mark_captcha_pending(f"{reason}:iframe")
        prompt_captcha_and_retry()
        return True
    return False

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
                LAST_ACTION["label"] = label
                LAST_ACTION["fn"] = lambda l=loc.first, t=timeout: l.click(timeout=t, force=True)
                if "submit" in label.lower():
                    log("[WAIT] Slowing down before final submission")
                    slow_wait(4)
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
                    if detect_captcha(page, reason="blocked_click"):
                        return True
    return False

def handle_resume_screen(page):
    resume_banner = page.locator("text=Add a resume for the employer")
    resume_card = page.locator("text=Use your Indeed Resume")
    upload_option = page.locator("text=Upload a resume")
    if resume_banner.count() or resume_card.count() or upload_option.count():
        log("[RESUME] Waiting for resume options")
        try:
            page.wait_for_selector("text=Use your Indeed Resume, text=Upload a resume", timeout=20000)
        except Exception as exc:
            log(f"[WARN] Resume options not visible yet: {exc}")
        log("[RESUME] Selecting Indeed resume")
        card = page.locator("text=Use your Indeed Resume").first
        try:
            card.scroll_into_view_if_needed()
            card.click(force=True)
        except Exception as exc:
            log(f"[WARN] Resume card click failed: {exc}")
            try:
                card.scroll_into_view_if_needed()
                card.click(force=True)
            except Exception as retry_exc:
                log(f"[WARN] Resume card retry failed: {retry_exc}")
        slow_wait(2)
        try:
            page.wait_for_selector("button:has-text('Continue')", timeout=20000)
        except Exception as exc:
            log(f"[WARN] Continue button not visible yet: {exc}")
        click_any(page, ["Continue"], timeout=20000)

def handle_radios(page):
    radios = page.locator("input[type='radio']")
    total = radios.count()
    groups = {}
    for i in range(total):
        radio = radios.nth(i)
        try:
            name = radio.get_attribute("name") or f"_unnamed_{i}"
            groups.setdefault(name, []).append(radio)
        except Exception as exc:
            log(f"[WARN] Radio group detection failed: {exc}")
    for name, group in groups.items():
        try:
            if any(r.is_checked() for r in group):
                continue
            selected = None
            for radio in group:
                try:
                    if not radio.is_visible() or not radio.is_enabled():
                        continue
                    selected = radio
                    break
                except Exception as exc:
                    log(f"[WARN] Radio visibility check failed: {exc}")
            if selected is None:
                continue
            label_text = ""
            try:
                label_text = selected.evaluate(
                    """e => {
                        const label = e.closest('label') || document.querySelector(`label[for='${e.id}']`);
                        return label ? label.innerText.trim() : '';
                    }"""
                )
            except Exception:
                label_text = ""
            selected.scroll_into_view_if_needed()
            selected.check(force=True)
            log(f"[RADIO] Selected first option for group {name}: {label_text or 'unlabeled'}")
        except Exception as exc:
            log(f"[WARN] Radio default failed for group {name}: {exc}")

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

def handle_distance_questions(page):
    keywords = ["commute", "commuting", "distance", "travel"]
    try:
        body = page.inner_text("body").lower()
    except Exception as exc:
        log(f"[WARN] Failed reading page text: {exc}")
        body = ""
    if not any(k in body for k in keywords):
        return
    log("[DISTANCE] Detected commuting question")
    radios = page.locator("input[type='radio']")
    for i in range(radios.count()):
        radio = radios.nth(i)
        try:
            label_text = radio.evaluate(
                """e => {
                    const label = e.closest('label') || document.querySelector(`label[for='${e.id}']`);
                    return label ? label.innerText.trim().toLowerCase() : '';
                }"""
            )
        except Exception:
            label_text = ""
        if "no" in label_text:
            try:
                radio.scroll_into_view_if_needed()
                radio.check(force=True)
                log(f"[DISTANCE] Selected radio: {label_text}")
                return
            except Exception as exc:
                log(f"[WARN] Distance radio select failed: {exc}")
    select = page.locator("select")
    if select.count():
        try:
            select.first.scroll_into_view_if_needed()
            select.first.select_option(label="No")
            log("[DISTANCE] Selected dropdown: No")
            return
        except Exception as exc:
            log(f"[WARN] Distance dropdown select failed: {exc}")
    inputs = page.locator("input[type='text'], textarea")
    for i in range(inputs.count()):
        el = inputs.nth(i)
        context = extract_context(el)
        if any(k in context for k in keywords):
            try:
                el.scroll_into_view_if_needed()
                el.fill("No")
                log("[DISTANCE] Filled input: No")
                return
            except Exception as exc:
                log(f"[WARN] Distance input fill failed: {exc}")

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

def update_progress_marker(page):
    url = page.url
    value = None
    try:
        progress = page.locator("[role='progressbar'], progress, [aria-valuenow]")
        if progress.count():
            value = progress.first.get_attribute("aria-valuenow") or progress.first.get_attribute("value")
    except Exception as exc:
        log(f"[WARN] Progress read failed: {exc}")
    LAST_PROGRESS["url"] = url
    LAST_PROGRESS["value"] = value
    LAST_PROGRESS["timestamp"] = time.time()

def check_for_stall(page, threshold=20):
    if LAST_PROGRESS["timestamp"] is None:
        update_progress_marker(page)
        return
    now = time.time()
    url = page.url
    value = None
    try:
        progress = page.locator("[role='progressbar'], progress, [aria-valuenow]")
        if progress.count():
            value = progress.first.get_attribute("aria-valuenow") or progress.first.get_attribute("value")
    except Exception as exc:
        log(f"[WARN] Progress read failed: {exc}")
    if url == LAST_PROGRESS["url"] and value == LAST_PROGRESS["value"] and now - LAST_PROGRESS["timestamp"] > threshold:
        log("[STALL] No progress detected, rescanning for actions/captcha")
        detect_captcha(page, reason="stall_detected")
        click_any(page, ["Continue", "Review", "Submit", "Submit your application"], timeout=8000)
        update_progress_marker(page)
    elif url != LAST_PROGRESS["url"] or value != LAST_PROGRESS["value"]:
        update_progress_marker(page)

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
        detect_captcha(page, reason="landing")
        detect_external(page)

        if not find_apply_button(page):
            log("[FAIL] No Apply CTA found")
            db_update(JOB_URL, "no_apply", "missing_apply_button")
            sys.exit(10)

        for step in range(30):
            log(f"[STEP] {step+1}")
            slow_wait(4)

            detect_invalid(page)
            detect_captcha(page, reason=f"step_{step+1}")
            detect_external(page)
            check_for_stall(page)

            body = page.inner_text("body").lower()
            if any(t in body for t in SUCCESS_TEXTS):
                log("[SUCCESS] Application submitted")
                db_update(JOB_URL, "applied")
                sys.exit(0)

            handle_resume_screen(page)
            handle_inputs(page)
            handle_distance_questions(page)
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
