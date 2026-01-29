import random
import re
import sqlite3
import sys
import time
import urllib.parse
from pathlib import Path

from playwright.sync_api import sync_playwright

DB_PATH = Path("jobs.db")

REMOTE_KEYWORDS = ["remote", "work from home", "wfh"]
NON_REMOTE_KEYWORDS = ["hybrid", "in person", "on-site", "estimated commute"]
STATE_CODES = {
    "al",
    "ak",
    "az",
    "ar",
    "ca",
    "co",
    "ct",
    "de",
    "fl",
    "ga",
    "hi",
    "id",
    "il",
    "in",
    "ia",
    "ks",
    "ky",
    "la",
    "me",
    "md",
    "ma",
    "mi",
    "mn",
    "ms",
    "mo",
    "mt",
    "ne",
    "nv",
    "nh",
    "nj",
    "nm",
    "ny",
    "nc",
    "nd",
    "oh",
    "ok",
    "or",
    "pa",
    "ri",
    "sc",
    "sd",
    "tn",
    "tx",
    "ut",
    "vt",
    "va",
    "wa",
    "wv",
    "wi",
    "wy",
    "dc",
}


if len(sys.argv) < 2:
    print("Usage: python scrape_jobs.py <indeed_search_url>")
    sys.exit(1)

SEARCH_URL = sys.argv[1]


def db_connect():
    return sqlite3.connect(DB_PATH)


def job_exists(conn, job_id):
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,))
    return cur.fetchone() is not None


def save_job(conn, job):
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO jobs (
            job_id, job_url, source, title, company, location,
            is_external, status, applied, attempts,
            last_attempt_at, last_error,
            created_at, updated_at, score, decision, notes
        ) VALUES (
            ?, ?, 'indeed', ?, ?, ?, ?, 'new', 0, 0,
            NULL, NULL,
            datetime('now'), datetime('now'), 0, 'apply', NULL
        )
        """,
        (
            job["job_id"],
            job["job_url"],
            job["title"],
            job["company"],
            job["location"],
            job["is_external"],
        ),
    )
    conn.commit()


def normalize_url(url):
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    if "jk" in qs:
        return f"https://www.indeed.com/viewjob?jk={qs['jk'][0]}"
    if url.startswith("/"):
        return "https://www.indeed.com" + url
    return url


def contains_city_state(text):
    if not text:
        return False
    lower = text.lower()
    for match in re.finditer(r"\b[a-z][a-z .'-]+,\s*([a-z]{2})\b", lower):
        if match.group(1) in STATE_CODES:
            return True
    return False


def is_remote_text(text):
    if not text:
        return False
    lower = text.lower()
    if any(bad in lower for bad in NON_REMOTE_KEYWORDS):
        return False
    return any(ok in lower for ok in REMOTE_KEYWORDS)


def is_non_remote_text(text):
    if not text:
        return False
    lower = text.lower()
    if any(bad in lower for bad in NON_REMOTE_KEYWORDS):
        return True
    if contains_city_state(text) and not any(ok in lower for ok in REMOTE_KEYWORDS):
        return True
    return False


def page_contains_expired(page):
    try:
        content = page.content().lower()
    except Exception:
        return False
    return "this job has expired" in content


def extract_company(card):
    company_loc = card.locator("span.companyName")
    if company_loc.count():
        return company_loc.first.inner_text().strip()
    company_loc = card.locator("[data-testid='company-name']")
    if company_loc.count():
        return company_loc.first.inner_text().strip()
    return "Unknown"


def extract_location(card):
    loc_loc = card.locator("span[data-testid='text-location']")
    if loc_loc.count():
        return loc_loc.first.inner_text().strip()
    loc_loc = card.locator(".companyLocation")
    if loc_loc.count():
        return loc_loc.first.inner_text().strip()
    loc_loc = card.locator("div[data-testid='text-location']")
    if loc_loc.count():
        return loc_loc.first.inner_text().strip()
    return "Unknown"


def extract_detail_location(page):
    loc_loc = page.locator("[data-testid='jobLocationText']")
    if loc_loc.count():
        return loc_loc.first.inner_text().strip()
    loc_loc = page.locator(".jobsearch-JobInfoHeader-subtitle")
    if loc_loc.count():
        return loc_loc.first.inner_text().strip()
    loc_loc = page.locator(".jobsearch-JobInfoHeader-subtitle div")
    if loc_loc.count():
        return loc_loc.first.inner_text().strip()
    return ""


def extract_jobs(page):
    cards = page.locator("a[data-jk]")
    jobs = []

    for i in range(cards.count()):
        time.sleep(random.uniform(0.5, 1.0))

        card = cards.nth(i)
        jk = card.get_attribute("data-jk")
        href = card.get_attribute("href")

        if not jk or not href:
            continue

        title = card.inner_text().split("\n")[0].strip()
        job_url = normalize_url(href)
        company = extract_company(card)
        location = extract_location(card)

        card_text = " ".join([title, location]).strip()
        if is_non_remote_text(card_text):
            print(f"⏭️ Skipped non-remote: {title} ({location})")
            continue

        remote_hint = is_remote_text(card_text)
        if not remote_hint:
            detail = page.context.new_page()
            detail.goto(job_url, timeout=30000)

            if page_contains_expired(detail):
                detail.close()
                print(f"⏭️ Skipped expired: {title}")
                continue

            detail_location = extract_detail_location(detail)
            if detail_location:
                location = detail_location

            try:
                detail_text = detail.inner_text("body")
            except Exception:
                detail_text = detail.content()

            if is_non_remote_text(detail_text) or is_non_remote_text(location):
                detail.close()
                print(f"⏭️ Skipped non-remote: {title} ({location})")
                continue

            if not is_remote_text(detail_text) and not is_remote_text(location):
                detail.close()
                print(f"⏭️ Skipped non-remote: {title} ({location})")
                continue

            detail.close()

        jobs.append(
            {
                "job_id": jk,
                "job_url": job_url,
                "title": title,
                "company": company,
                "location": location,
                "is_external": 0,
            }
        )

    return jobs


with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://localhost:9223")
    context = browser.contexts[0]
    page = context.pages[0] if context.pages else context.new_page()

    page.goto(SEARCH_URL, timeout=60000)

    conn = db_connect()
    page_num = 1
    added = 0

    while True:
        print(f"\n--- PAGE {page_num} ---")
        jobs = extract_jobs(page)

        for job in jobs:
            if job_exists(conn, job["job_id"]):
                print(f"⏭️ Skipped duplicate: {job['job_id']}")
                continue
            save_job(conn, job)
            added += 1
            print(f"💾 Inserted: {job['title']}")

        next_btn = page.locator("a[aria-label='Next Page']")
        if next_btn.count() == 0:
            break

        next_btn.first.click()
        time.sleep(random.uniform(2.5, 3.5))
        page_num += 1

    conn.close()

print(f"\n✅ Scrape complete. New remote jobs added: {added}")
