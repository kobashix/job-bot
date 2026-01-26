import sys
import time
import json
import sqlite3
import config
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
    "bamboohr",
    "jobvite",
]
CAPTCHA_IFRAME_SELECTORS = [
    "iframe[src*='captcha' i]",
    "iframe[src*='recaptcha' i]",
    "iframe[src*='hcaptcha' i]",
    "iframe[src*='api2/anchor' i]",
    "iframe[src*='api2/bframe' i]",
    "iframe[title*='captcha' i]",
    "iframe[title*='recaptcha' i]",
    "iframe[title*='hcaptcha' i]",
    "div[aria-label*='captcha' i]",
    "textarea[name='g-recaptcha-response' i]",
    "textarea[name='h-captcha-response' i]",
    "input[name='g-recaptcha-response' i]",
    "input[name='h-captcha-response' i]",
    ".grecaptcha-badge",
    ".h-captcha",
    "div[id*='recaptcha' i]",
    "div[class*='recaptcha' i]",
]
CAPTCHA_TEXT_LOCATOR = "text=/i'm not a robot|verify you are human|security check|captcha/i"
CAPTCHA_CHALLENGE_LOCATOR = "iframe[title*='challenge' i], iframe[src*='challenge' i]"

EXTERNAL_BUTTON_LABELS = ["Apply on company site", "Apply externally"]
EXTERNAL_ACTION_LABELS = ["Apply", "Next", "Continue"]
EXTERNAL_FINAL_SUBMIT_LABELS = ["Submit application", "Finish", "Complete application"]

LAST_ACTION = {"label": None, "fn": None}
LAST_PROGRESS = {"url": None, "value": None, "timestamp": None}

def log(msg):
    print(msg, flush=True)

def db_update(job_url, status, reason=None, is_external=None):
    attempts = 0
    while True:
        try:
            with sqlite3.connect(DB_PATH, timeout=30) as conn:
                fields = [
                    "applied = ?",
                    "status = ?",
                    "last_error = ?",
                    "updated_at = CURRENT_TIMESTAMP",
                ]
                values = [1 if status == "applied" else 0, status, reason]
                if is_external is not None:
                    fields.append("is_external = ?")
                    values.append(1 if is_external else 0)
                values.append(job_url)
                conn.execute(
                    f"""
                    UPDATE jobs
                    SET {", ".join(fields)}
                    WHERE job_url = ?
                    """,
                    values,
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

def db_delete(job_url):
    attempts = 0
    while True:
        try:
            with sqlite3.connect(DB_PATH, timeout=30) as conn:
                conn.execute(
                    """
                    DELETE FROM jobs
                    WHERE job_url = ?
                    """,
                    (job_url,),
                )
                conn.commit()
            log(f"[DB] Deleted job: {job_url}")
            return
        except sqlite3.OperationalError as exc:
            attempts += 1
            log(f"[DB] Delete failed ({attempts}): {exc}")
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

def format_external_reason(final_url, ats, reason):
    ts = datetime.now().isoformat()
    return f"{reason}|ats={ats}|url={final_url}|ts={ts}"

def mark_external_site(final_url, ats, reason="external_detected"):
    db_update(JOB_URL, "external", format_external_reason(final_url, ats, reason), is_external=True)

def locator_has_visible(locator, limit=5):
    try:
        count = min(locator.count(), limit)
    except Exception:
        return False
    for i in range(count):
        try:
            if locator.nth(i).is_visible():
                return True
        except Exception:
            continue
    return False

def mark_captcha_pending(reason, is_external=False):
    log(f"[CAPTCHA] {reason}")
    db_update(JOB_URL, "captcha_pending", reason, is_external=is_external)

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

def detect_captcha(page, reason="captcha_detected", is_external=False, allow_retry=True):
    body = ""
    title = ""
    try:
        body = page.inner_text("body").lower()
        title = page.title().lower()
    except Exception as exc:
        log(f"[WARN] Failed reading page text: {exc}")
    text_locator = page.locator(CAPTCHA_TEXT_LOCATOR)
    footer_only = "protected by recaptcha" in body and "google privacy policy" in body
    if locator_has_visible(text_locator):
        strong_text = any(t in body for t in ["i'm not a robot", "verify you are human", "security check"])
        if footer_only and not strong_text:
            log("[CAPTCHA] Footer recaptcha notice detected; no challenge visible")
            return False
        else:
            if allow_retry:
                log("[CAPTCHA] Challenge text detected; retrying action before prompt")
                if click_any(page, ["Continue", "Review", "Submit", "Submit your application"], timeout=8000):
                    slow_wait(2)
                    return detect_captcha(page, reason=reason, is_external=is_external, allow_retry=False)
            mark_captcha_pending(f"{reason}:visible_text")
            prompt_captcha_and_retry()
            return True
    for t in BLOCK_TEXTS:
        if t in ["captcha"]:
            if t in body or t in title:
                if footer_only and not page.locator(CAPTCHA_CHALLENGE_LOCATOR).count():
                    log("[CAPTCHA] Footer recaptcha notice detected; no challenge visible")
                    return False
                if locator_has_visible(text_locator) or page.locator(CAPTCHA_CHALLENGE_LOCATOR).count():
                    if allow_retry:
                        log("[CAPTCHA] Challenge indicators detected; retrying action before prompt")
                        if click_any(page, ["Continue", "Review", "Submit", "Submit your application"], timeout=8000):
                            slow_wait(2)
                            return detect_captcha(page, reason=reason, is_external=is_external, allow_retry=False)
                    mark_captcha_pending(f"{reason}:{t}", is_external=is_external)
                    prompt_captcha_and_retry()
                    return True
        else:
            if t in body or t in title:
                mark_captcha_pending(f"{reason}:{t}", is_external=is_external)
                prompt_captcha_and_retry()
                return True
    captcha_frames = page.locator(", ".join(CAPTCHA_IFRAME_SELECTORS))
    if captcha_frames.count() and locator_has_visible(captcha_frames):
        if allow_retry:
            log("[CAPTCHA] Captcha widget visible; retrying action before prompt")
            if click_any(page, ["Continue", "Review", "Submit", "Submit your application"], timeout=8000):
                slow_wait(2)
                return detect_captcha(page, reason=reason, is_external=is_external, allow_retry=False)
        mark_captcha_pending(f"{reason}:iframe_visible", is_external=is_external)
        prompt_captcha_and_retry()
        return True
    challenge_frames = page.locator(CAPTCHA_CHALLENGE_LOCATOR)
    if challenge_frames.count():
        if allow_retry:
            log("[CAPTCHA] Challenge frame detected; retrying action before prompt")
            if click_any(page, ["Continue", "Review", "Submit", "Submit your application"], timeout=8000):
                slow_wait(2)
                return detect_captcha(page, reason=reason, is_external=is_external, allow_retry=False)
        mark_captcha_pending(f"{reason}:challenge_frame", is_external=is_external)
        prompt_captcha_and_retry()
        return True
    return False

def is_indeed_host(host):
    return host.endswith("indeed.com") or host == "smartapply.indeed.com"

def detect_ats(final_url, page):
    lower_url = final_url.lower()
    ats_map = [
        ("workday", "Workday"),
        ("greenhouse", "Greenhouse"),
        ("lever", "Lever"),
        ("icims", "iCIMS"),
        ("paycom", "Paycom"),
        ("adp", "ADP"),
        ("bamboohr", "BambooHR"),
        ("taleo", "Taleo"),
        ("jobvite", "Jobvite"),
    ]
    for token, name in ats_map:
        if token in lower_url:
            return name
    try:
        body = page.inner_text("body").lower()
    except Exception as exc:
        log(f"[WARN] ATS detection failed: {exc}")
        body = ""
    for token, name in ats_map:
        if token in body:
            return name
    return "unknown_external"

def detect_external_context(page, context):
    current_url = page.url
    host = urlparse(current_url).netloc.lower()
    if host and not is_indeed_host(host):
        return {"page": page, "final_url": current_url, "reason": "host_change"}
    for label in EXTERNAL_BUTTON_LABELS:
        button = page.locator(f"button:has-text('{label}')")
        if locator_has_visible(button):
            return {"page": page, "final_url": current_url, "reason": f"button:{label}", "button": label}
    for extra_page in context.pages:
        try:
            extra_url = extra_page.url
        except Exception:
            continue
        extra_host = urlparse(extra_url).netloc.lower()
        if extra_host and not is_indeed_host(extra_host):
            return {"page": extra_page, "final_url": extra_url, "reason": "new_tab"}
    if any(k in host for k in EXTERNAL_HOST_KEYWORDS):
        return {"page": page, "final_url": current_url, "reason": "keyword_host"}
    return None

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
        "not found",
    ]
    for text in invalid_texts:
        if text in body or text in title:
            log(f"[INVALID] {text}")
            db_update(JOB_URL, "invalid", f"invalid_text:{text}")
            sys.exit(12)

def detect_not_found_and_delete(page):
    try:
        body = page.inner_text("body").lower()
        title = page.title().lower()
    except Exception as exc:
        log(f"[WARN] Failed reading page text: {exc}")
        return False
    if "not found" in body or "not found" in title:
        log("[INVALID] Not Found page detected; deleting job")
        db_delete(JOB_URL)
        sys.exit(13)
    return False

def handle_additional_verification(page, timeout_sec=30):
    try:
        body = page.inner_text("body").lower()
        title = page.title().lower()
    except Exception as exc:
        log(f"[WARN] Failed reading page text: {exc}")
        return False
    if "additional verification needed" in body or "additional verification needed" in title:
        log("[CAPTCHA] Additional verification needed; solve captcha then press ENTER")
        mark_captcha_pending("additional_verification")
        try:
            import threading
            user_input = {"done": False}
            def wait_for_input():
                try:
                    input()
                    user_input["done"] = True
                except EOFError:
                    log("[CAPTCHA] No stdin available to continue")
            thread = threading.Thread(target=wait_for_input, daemon=True)
            thread.start()
            thread.join(timeout=timeout_sec)
            if not user_input["done"]:
                log("[CAPTCHA] Verification timeout; moving to next job")
                db_update(JOB_URL, "captcha_pending", "verification_timeout")
                sys.exit(21)
            log("[CAPTCHA] Verification acknowledged; continuing")
            return True
        except Exception as exc:
            log(f"[WARN] Verification prompt failed: {exc}")
            db_update(JOB_URL, "captcha_pending", "verification_prompt_failed")
            sys.exit(21)
    return False

def click_any(page, labels, timeout=5000):
    for label in labels:
        loc = page.locator(f"button:has-text('{label}')")
        if loc.count():
            try:
                loc.first.scroll_into_view_if_needed()
                LAST_ACTION["label"] = label
                LAST_ACTION["fn"] = lambda l=loc.first, t=timeout: l.click(timeout=t, force=True)
                try:
                    if not loc.first.is_enabled():
                        log(f"[WARN] Button disabled: {label}")
                except Exception as exc:
                    log(f"[WARN] Button enabled check failed for {label}: {exc}")
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
                    if detect_captcha(page, reason="blocked_click", allow_retry=False):
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
            elif "what industries have you supported" in context:
                el.scroll_into_view_if_needed()
                el.fill(
                    "I have previous experience in healthcare, education, state and local government, "
                    "tourism, construction, and general small business areas."
                )
                log("[INPUT] Filled industries supported")
            elif "how large was the organization you supported" in context:
                el.scroll_into_view_if_needed()
                el.fill("I currently support a national organization within 100s of locations and thousands of employees.")
                log("[INPUT] Filled organization size")
            elif "size of team managed" in context:
                el.scroll_into_view_if_needed()
                el.fill("20+ team members managed.")
                log("[INPUT] Filled team size")
            elif "do you have" in context and "degree" in context:
                el.scroll_into_view_if_needed()
                el.fill("I have an MBA and MAcc, also a CPA.")
                log("[INPUT] Filled degree response")
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

def handle_relevant_experience(page):
    option_text = "Controller National Park College"
    try:
        if page.locator(f"text={option_text}").count():
            loc = page.locator(f"text={option_text}").first
            loc.scroll_into_view_if_needed()
            try:
                loc.click(force=True)
            except Exception as exc:
                log(f"[WARN] Experience text click failed: {exc}")
            input_loc = page.locator("input[type='radio'], input[type='checkbox']").filter(has=page.locator(f"text={option_text}"))
            if input_loc.count():
                input_loc.first.check(force=True)
            log(f"[EXPERIENCE] Selected {option_text}")
    except Exception as exc:
        log(f"[WARN] Experience selection failed: {exc}")

def find_apply_button(page):
    return click_any(page, ["Apply", "Apply now", "Apply on company site"], timeout=15000)

def external_capture_screenshot(page, reason):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = config.ARTIFACT_DIR / f"external_failure_{timestamp}.png"
    try:
        page.screenshot(path=str(filename), full_page=True)
        log(f"[EXTERNAL] Screenshot saved: {filename} ({reason})")
    except Exception as exc:
        log(f"[WARN] Screenshot capture failed: {exc}")

def external_fail(status, reason, page, final_url, ats):
    external_capture_screenshot(page, reason)
    db_update(JOB_URL, status, format_external_reason(final_url, ats, reason), is_external=True)
    log(f"[EXTERNAL] Failure: {status} - {reason}")
    sys.exit(30)

def external_confirm_submit():
    log("READY TO SUBMIT EXTERNAL APPLICATION – press ENTER to continue")
    try:
        input()
        return True
    except EOFError:
        log("[EXTERNAL] Submission confirmation unavailable (stdin closed)")
        return False

def external_fill_inputs(page):
    email = getattr(config, "APPLY_EMAIL", None)
    phone = getattr(config, "APPLY_PHONE", None)
    inputs = page.locator("input[type='text'], input[type='email'], input[type='tel'], textarea")
    for i in range(inputs.count()):
        el = inputs.nth(i)
        context = extract_context(el)
        try:
            if "first name" in context:
                el.scroll_into_view_if_needed()
                el.fill("Andrew")
                log("[EXTERNAL] Filled first name")
            elif "last name" in context:
                el.scroll_into_view_if_needed()
                el.fill("Pennington")
                log("[EXTERNAL] Filled last name")
            elif "full name" in context or "your name" in context:
                el.scroll_into_view_if_needed()
                el.fill("Andrew Pennington")
                log("[EXTERNAL] Filled full name")
            elif "email" in context:
                if email:
                    el.scroll_into_view_if_needed()
                    el.fill(email)
                    log("[EXTERNAL] Filled email")
                else:
                    log("[WARN] APPLY_EMAIL missing in config")
            elif "phone" in context:
                if phone:
                    el.scroll_into_view_if_needed()
                    el.fill(phone)
                    log("[EXTERNAL] Filled phone")
                else:
                    log("[WARN] APPLY_PHONE missing in config")
            elif "how many years" in context:
                el.scroll_into_view_if_needed()
                el.fill("15")
                log("[EXTERNAL] Filled years")
        except Exception as exc:
            log(f"[WARN] External input fill failed: {exc}")

def external_select_dropdowns(page):
    selects = page.locator("select")
    for i in range(selects.count()):
        select = selects.nth(i)
        try:
            options = select.locator("option")
            chosen = None
            for j in range(options.count()):
                opt = options.nth(j)
                value = opt.get_attribute("value")
                text = opt.inner_text().strip()
                if not value or "select" in text.lower() or "choose" in text.lower():
                    continue
                chosen = value
                break
            if chosen:
                select.scroll_into_view_if_needed()
                select.select_option(value=chosen)
                log(f"[EXTERNAL] Selected dropdown option: {chosen}")
        except Exception as exc:
            log(f"[WARN] External dropdown select failed: {exc}")

def external_handle_radios(page):
    keywords_no = ["commute", "commuting", "distance", "travel", "relocation", "relocate", "sponsorship", "visa"]
    radios = page.locator("input[type='radio']")
    total = radios.count()
    groups = {}
    for i in range(total):
        radio = radios.nth(i)
        try:
            name = radio.get_attribute("name") or f"_unnamed_{i}"
            groups.setdefault(name, []).append(radio)
        except Exception as exc:
            log(f"[WARN] External radio group detection failed: {exc}")
    for name, group in groups.items():
        try:
            if any(r.is_checked() for r in group):
                continue
            group_text = ""
            try:
                group_text = group[0].evaluate("e => e.closest('fieldset')?.innerText?.toLowerCase() || ''")
            except Exception:
                group_text = ""
            prefer_no = any(k in group_text for k in keywords_no)
            selected = None
            if prefer_no:
                for radio in group:
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
                        selected = radio
                        break
            if selected is None:
                for radio in group:
                    try:
                        if not radio.is_visible() or not radio.is_enabled():
                            continue
                        selected = radio
                        break
                    except Exception:
                        continue
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
            log(f"[EXTERNAL] Selected radio for group {name}: {label_text or 'unlabeled'}")
        except Exception as exc:
            log(f"[WARN] External radio selection failed for group {name}: {exc}")

def external_handle_demographics(page):
    keywords = ["gender", "ethnicity", "race", "veteran", "disability"]
    try:
        body = page.inner_text("body").lower()
    except Exception as exc:
        log(f"[WARN] External demographics check failed: {exc}")
        body = ""
    if not any(k in body for k in keywords):
        return
    selections = [
        ("gender", "Male"),
        ("ethnicity", "Not Hispanic or Latino"),
        ("race", "White"),
        ("veteran", "No"),
        ("disability", "No, I do not have a disability and have not had one in the past"),
    ]
    matched = False
    for key, val in selections:
        if key in body:
            loc = page.locator(f"label:has-text('{val}')")
            if loc.count():
                matched = True
                try:
                    loc.first.scroll_into_view_if_needed()
                    loc.first.click(force=True)
                    log(f"[EXTERNAL] Demographic selected {val}")
                except Exception as exc:
                    log(f"[WARN] External demographic selection failed for {val}: {exc}")
    if not matched:
        log("[EXTERNAL] demographic_skipped")

def external_click_actions(page):
    for label in EXTERNAL_ACTION_LABELS:
        loc = page.locator(f"button:has-text('{label}')")
        if locator_has_visible(loc):
            try:
                loc.first.scroll_into_view_if_needed()
                loc.first.click(timeout=15000)
                log(f"[EXTERNAL] Clicked {label}")
                return True
            except Exception as exc:
                log(f"[WARN] External click failed on {label}: {exc}")
                try:
                    loc.first.scroll_into_view_if_needed()
                    loc.first.click(timeout=15000, force=True)
                    log(f"[EXTERNAL] Retry click success: {label}")
                    return True
                except Exception as retry_exc:
                    log(f"[WARN] External retry click failed on {label}: {retry_exc}")
    return False

def external_handle_submit(page):
    for label in EXTERNAL_FINAL_SUBMIT_LABELS:
        loc = page.locator(f"button:has-text('{label}')")
        if locator_has_visible(loc):
            if not external_confirm_submit():
                return "confirmation_unavailable"
            try:
                loc.first.scroll_into_view_if_needed()
                loc.first.click(timeout=20000)
                log(f"[EXTERNAL] Clicked final submit: {label}")
                return "submitted"
            except Exception as exc:
                log(f"[WARN] External submit click failed: {exc}")
                try:
                    loc.first.scroll_into_view_if_needed()
                    loc.first.click(timeout=20000, force=True)
                    log(f"[EXTERNAL] Retry final submit: {label}")
                    return "submitted"
                except Exception as retry_exc:
                    log(f"[WARN] External submit retry failed: {retry_exc}")
                    return "navigation_blocked"
    return None

def external_detect_required_errors(page):
    try:
        body = page.inner_text("body").lower()
    except Exception as exc:
        log(f"[WARN] External required check failed: {exc}")
        return False
    return "required" in body or "please fill" in body or "missing" in body

def external_apply_handler(page, context, external_info):
    active_page = external_info["page"]
    initial_url = active_page.url
    if external_info.get("button"):
        label = external_info["button"]
        log(f"[EXTERNAL] Triggered by button: {label}")
        button = active_page.locator(f"button:has-text('{label}')")
        try:
            button.first.scroll_into_view_if_needed()
            button.first.click(timeout=15000)
        except Exception as exc:
            log(f"[WARN] External apply button click failed: {exc}")
            try:
                button.first.scroll_into_view_if_needed()
                button.first.click(timeout=15000, force=True)
            except Exception as retry_exc:
                log(f"[WARN] External apply button retry failed: {retry_exc}")
        try:
            new_page = context.wait_for_event("page", timeout=15000)
            active_page = new_page
        except Exception as exc:
            log(f"[WARN] No new tab detected after external apply button: {exc}")
    final_url = active_page.url
    ats = detect_ats(final_url, active_page)
    log(f"[EXTERNAL] Detected ATS: {ats} URL: {final_url}")
    mark_external_site(final_url, ats, external_info.get("reason", "external_detected"))

    for step in range(30):
        log(f"[EXTERNAL STEP] {step+1} URL: {active_page.url} ATS: {ats}")
        slow_wait(3)
        detect_captcha(active_page, reason=f"external_step_{step+1}", is_external=True)
        external_fill_inputs(active_page)
        external_select_dropdowns(active_page)
        external_handle_radios(active_page)
        external_handle_demographics(active_page)

        submit_result = external_handle_submit(active_page)
        if submit_result == "confirmation_unavailable":
            external_fail("navigation_blocked", "confirmation_unavailable", active_page, final_url, ats)
        if submit_result == "navigation_blocked":
            external_fail("navigation_blocked", "submit_blocked", active_page, final_url, ats)
        if submit_result == "submitted":
            slow_wait(4)
            try:
                body = active_page.inner_text("body").lower()
            except Exception as exc:
                log(f"[WARN] External success check failed: {exc}")
                body = ""
            if any(t in body for t in SUCCESS_TEXTS):
                db_update(JOB_URL, "applied", format_external_reason(active_page.url, ats, "external_applied"), is_external=True)
                log("[EXTERNAL] Application submitted")
                sys.exit(0)
            db_update(JOB_URL, "external_submitted", format_external_reason(active_page.url, ats, "submitted_no_confirmation"), is_external=True)
            log("[EXTERNAL] Submitted without confirmation text")
            sys.exit(0)

        if external_click_actions(active_page):
            continue

        if external_detect_required_errors(active_page):
            external_fail("missing_required_fields", "required_fields", active_page, final_url, ats)

        log("[EXTERNAL] No actionable button found")
        slow_wait(5)

    external_fail("unsupported_external_flow", "max_steps", active_page, final_url, ats)

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
        buttons = page.locator("button:has-text('Continue'), button:has-text('Review'), button:has-text('Submit')")
        if buttons.count():
            try:
                if not buttons.first.is_enabled():
                    log("[STALL] Action button present but disabled")
            except Exception as exc:
                log(f"[WARN] Action button enabled check failed: {exc}")
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
        detect_not_found_and_delete(page)
        handle_additional_verification(page)
        detect_captcha(page, reason="landing")
        external_info = detect_external_context(page, ctx)
        if external_info:
            external_apply_handler(page, ctx, external_info)
            sys.exit(0)

        if not find_apply_button(page):
            log("[FAIL] No Apply CTA found")
            db_update(JOB_URL, "no_apply", "missing_apply_button")
            sys.exit(10)

        for step in range(30):
            log(f"[STEP] {step+1}")
            slow_wait(4)

            detect_invalid(page)
            detect_not_found_and_delete(page)
            handle_additional_verification(page)
            detect_captcha(page, reason=f"step_{step+1}")
            external_info = detect_external_context(page, ctx)
            if external_info:
                external_apply_handler(page, ctx, external_info)
                sys.exit(0)
            check_for_stall(page)

            body = page.inner_text("body").lower()
            if any(t in body for t in SUCCESS_TEXTS):
                log("[SUCCESS] Application submitted")
                db_update(JOB_URL, "applied")
                sys.exit(0)

            handle_resume_screen(page)
            handle_inputs(page)
            handle_relevant_experience(page)
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
