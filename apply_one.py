import sys
import time
from playwright.sync_api import TimeoutError as PWTimeout
from config import ARTIFACT_DIR, SLEEP_BETWEEN_STEPS_SEC, MAX_STEPS_PER_APPLICATION
from db import start_run, end_run, bump_attempt, set_job_status, log_event
from answers_sqlite import find_answer, train_answer
from browser_cdp import get_cdp_page

def save_artifacts(page, prefix: str):
    ts = int(time.time())
    png = ARTIFACT_DIR / f"{prefix}_{ts}.png"
    html = ARTIFACT_DIR / f"{prefix}_{ts}.html"
    try:
        page.screenshot(path=str(png), full_page=True)
    except Exception:
        pass
    try:
        html.write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    return f"{prefix}_{ts}"

def detect_block(page) -> bool:
    return (
        page.get_by_text("Additional Verification Required").count() > 0
        or page.get_by_text("Verify you are human").count() > 0
    )

def answer_yes_no_group(container, train_mode: bool):
    qtext = container.inner_text()
    match = find_answer(qtext)
    if not match and train_mode:
        print("\nTRAIN MODE: Unknown yes/no question:\n", qtext)
        val = input("Type answer (Yes/No): ").strip() or "Yes"
        key = input("Type key name (e.g. work_authorization_us): ").strip() or "custom_yesno"
        aliases = [qtext.lower()]
        train_answer(key, val, aliases)
        match = (key, val)

    if not match:
        return False, "UNKNOWN_QUESTION"

    _, val = match
    # Try click label matching value
    lbl = container.locator(f"label:has-text('{val}')")
    if lbl.count():
        lbl.first.click(force=True)
        return True, "ANSWERED"
    return False, "CHOICE_NOT_FOUND"

def fill_text_inputs(page, train_mode: bool):
    # Fill all visible required text/number inputs with known defaults where possible
    inputs = page.locator("input[type='text'], input[type='number'], textarea")
    for i in range(inputs.count()):
        el = inputs.nth(i)
        if not el.is_visible():
            continue
        # Try to find surrounding question text
        q = el.evaluate("e => e.closest('div')?.innerText || ''")
        match = find_answer(q)
        if match:
            _, val = match
            el.fill(val)
        elif train_mode:
            print("\nTRAIN MODE: Unknown text question:\n", q)
            val = input("Type answer value: ").strip()
            key = input("Type key name (e.g. experience_years): ").strip() or "custom_text"
            train_answer(key, val, [q.lower()])
            el.fill(val)

def main():
    if len(sys.argv) < 2:
        print("Usage: python apply_one.py [--train] <job_url>")
        sys.exit(1)

    train_mode = "--train" in sys.argv
    job_url = sys.argv[-1]
    run_id = start_run("apply_one", notes=job_url)

    p, browser, context, page = get_cdp_page()
    try:
        page.goto(job_url, timeout=60000)
        page.wait_for_timeout(2000)

        if detect_block(page):
            ap = save_artifacts(page, "cloudflare_block")
            log_event(run_id, "ERROR", "CLOUDFLARE_BLOCK", url=page.url, artifact_prefix=ap)
            print("PAUSE: Cloudflare/verification. Solve it in Chromium, then press ENTER here.")
            input()
            page.wait_for_timeout(2000)

        # If already applied
        if page.get_by_text("Applied").count() > 0 or page.get_by_text("You applied").count() > 0:
            print("ALREADY_APPLIED")
            return

        # Apply button
        apply_btn = page.locator("button:has-text('Apply')")
        if apply_btn.count() == 0:
            # external apply on company site
            if page.get_by_text("Apply on company site").count() > 0:
                print("EXTERNAL_SITE")
            else:
                ap = save_artifacts(page, "no_apply_button")
                log_event(run_id, "ERROR", "NO_APPLY_BUTTON", url=page.url, artifact_prefix=ap)
            return

        apply_btn.first.click(force=True)
        page.wait_for_timeout(2000)

        # External ATS detection: not smartapply domain
        if "smartapply.indeed.com" not in page.url:
            ap = save_artifacts(page, "external_ats")
            log_event(run_id, "INFO", "EXTERNAL_ATS", url=page.url, artifact_prefix=ap)
            print("EXTERNAL_ATS")
            return

        # Step loop
        for step in range(MAX_STEPS_PER_APPLICATION):
            page.wait_for_timeout(int(SLEEP_BETWEEN_STEPS_SEC * 1000))

            if detect_block(page):
                ap = save_artifacts(page, "cloudflare_midflow")
                log_event(run_id, "ERROR", "CLOUDFLARE_BLOCK_MIDFLOW", url=page.url, artifact_prefix=ap)
                print("PAUSE: verification mid-flow. Solve and press ENTER.")
                input()
                continue

            # Resume screen
            if page.get_by_text("Add a resume for the employer").count() > 0:
                fill_text_inputs(page, train_mode)
                btn = page.locator("[data-testid^='hp-continue-button'], button:has-text('Continue')")
                if btn.count():
                    btn.last.click(force=True)
                    continue

            # EEO screen (optional): gender/race
            if page.get_by_text("Voluntary self identification").count() > 0:
                # best effort using stored answers
                for label in ["Male", "White"]:
                    l = page.locator(f"label:has-text('{label}')")
                    if l.count():
                        l.first.click(force=True)
                # try review button
                rb = page.locator("button:has-text('Review')")
                if rb.count():
                    rb.first.click(force=True)
                    continue

            # Yes/No questions groups
            fieldsets = page.locator("fieldset")
            for i in range(fieldsets.count()):
                fs = fieldsets.nth(i)
                if fs.locator("input[type='radio']").count():
                    ok, _ = answer_yes_no_group(fs, train_mode)

            # Text inputs
            fill_text_inputs(page, train_mode)

            # Submit
            submit = page.locator("button[type='submit']:has-text('Submit'), button:has-text('Submit your application')")
            if submit.count():
                submit.first.click(force=True)
                page.wait_for_timeout(3000)
                if page.get_by_text("has been submitted").count():
                    print("SUBMITTED")
                    return

            # Continue/Next
            cont = page.locator("button:has-text('Continue applying'), button:has-text('Continue'), button:has-text('Next'), [data-testid^='hp-continue-button']")
            if cont.count():
                cont.last.click(force=True)
                continue

            ap = save_artifacts(page, "stalled")
            log_event(run_id, "ERROR", f"STALLED step={step}", url=page.url, artifact_prefix=ap)
            print("STALLED")
            return

        ap = save_artifacts(page, "step_limit")
        log_event(run_id, "ERROR", "STEP_LIMIT", url=page.url, artifact_prefix=ap)
        print("STEP_LIMIT")
    finally:
        end_run(run_id)
        p.stop()

if __name__ == "__main__":
    main()