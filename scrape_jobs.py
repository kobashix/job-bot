import sys
import time
import sqlite3
import urllib.parse
from pathlib import Path
from playwright.sync_api import sync_playwright

DB_PATH = Path("jobs.db")
SEARCH_URL = sys.argv[1]

# -----------------------
# DATABASE
# -----------------------

def db_connect():
    print("🗄️ Connecting to jobs.db")
    return sqlite3.connect(DB_PATH)

def job_exists_by_id(conn, job_id):
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,))
    return cur.fetchone() is not None

def save_job(conn, job):
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO jobs (
                job_id,
                job_url,
                source,
                title,
                company,
                location,
                is_external,
                status,
                applied,
                attempts,
                last_attempt_at,
                last_error,
                created_at,
                updated_at,
                score,
                decision,
                notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'new', 0, 0, NULL, NULL, datetime('now'), datetime('now'), 0, 'apply', NULL)
        """, (
            job["job_id"],
            job["job_url"],
            "indeed",
            job["title"],
            job["company"],
            job["location"],
            job["is_external"]
        ))
        conn.commit()
        print(f"💾 Saved job_id={job['job_id']}")
        return True
    except sqlite3.IntegrityError:
        print(f"⏭️ Duplicate job_id skipped: {job['job_id']}")
        return False

# -----------------------
# HELPERS
# -----------------------

def normalize_indeed_url(url):
    parsed = urllib.parse.urlparse(url)
    if "/pagead/clk" in parsed.path:
        qs = urllib.parse.parse_qs(parsed.query)
        if "jk" in qs:
            return f"https://www.indeed.com/viewjob?jk={qs['jk'][0]}"
    if url.startswith("/"):
        return "https://www.indeed.com" + url
    return url

def detect_captcha(page):
    content = page.content().lower()
    if "additional verification" in content or "verify you are human" in content:
        print("\n🛑 CAPTCHA DETECTED")
        input("👉 Solve captcha in browser, then press ENTER here...")

def extract_jobs(page):
    cards = page.locator("a[data-jk]")
    count = cards.count()
    print(f"📦 Found {count} job cards")

    jobs = []
    for i in range(count):
        card = cards.nth(i)
        jk = card.get_attribute("data-jk")
        href = card.get_attribute("href")

        if not jk or not href:
            continue

        job_url = normalize_indeed_url(href)
        title = card.inner_text().split("\n")[0].strip()
        location = "Unknown"
        location_locators = [
            "span[data-testid='text-location']",
            ".companyLocation",
            "div[data-testid='text-location']",
        ]
        for selector in location_locators:
            loc = card.locator(selector)
            if loc.count():
                location = loc.first.inner_text().strip()
                break
        if "remote" not in location.lower():
            print(f"⏭️ Skipping non-remote job: {title} ({location})")
            continue

        jobs.append({
            "job_id": jk,
            "job_url": job_url,
            "title": title,
            "company": "Unknown",
            "location": location,
            "is_external": 0
        })

    return jobs

# -----------------------
# MAIN
# -----------------------

print("\nOPENING SEARCH:")
print(SEARCH_URL)

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://localhost:9223")
    context = browser.contexts[0]
    page = context.pages[0] if context.pages else context.new_page()

    page.goto(SEARCH_URL, timeout=60000)
    detect_captcha(page)

    conn = db_connect()

    page_num = 1
    total_added = 0

    while True:
        print(f"\n--- SCRAPING PAGE {page_num} ---")

        jobs = extract_jobs(page)

        for idx, job in enumerate(jobs, 1):
            print(f"\n➡️ Processing job {idx}/{len(jobs)}")
            if job_exists_by_id(conn, job["job_id"]):
                print("⏭️ Already exists by job_id")
                continue

            if save_job(conn, job):
                total_added += 1

        next_btn = page.locator("a[aria-label='Next Page']")
        if next_btn.count() == 0:
            print("\n🚫 NO NEXT PAGE. STOPPING.")
            break

        print("➡️ Going to next page...")
        next_btn.first.click()
        time.sleep(2)
        detect_captcha(page)
        page_num += 1

    conn.close()

print("\n=== SCRAPE COMPLETE ===")
print(f"✅ New jobs added this run: {total_added}")
