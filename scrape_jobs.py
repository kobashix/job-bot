import asyncio
import random
import sys
import urllib.parse
from playwright.async_api import async_playwright
from helpers.db import DBClient
from helpers.utils import setup_logging, load_config, ContextLogger

# Constants for filtering (simplified for the redo)
REMOTE_KEYWORDS = ["remote", "work from home", "wfh"]
NON_REMOTE_KEYWORDS = ["hybrid", "in person", "on-site", "estimated commute"]

def normalize_url(url):
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    if "jk" in qs:
        return f"https://www.indeed.com/viewjob?jk={qs['jk'][0]}"
    if url.startswith("/"):
        return "https://www.indeed.com" + url
    return url

def is_remote(text):
    if not text: return False
    l = text.lower()
    return any(k in l for k in REMOTE_KEYWORDS) and not any(k in l for k in NON_REMOTE_KEYWORDS)

async def scrape(search_url: str):
    logger = setup_logging()
    ctx_logger = ContextLogger(logger, {"step": "scrape"})
    config = load_config("config.json")
    db = DBClient(str(config.db_path), ctx_logger)
    
    async with async_playwright() as p:
        # Use existing CDP browser if available, else launch
        try:
            browser = await p.chromium.connect_over_cdp("http://localhost:9223")
            context = browser.contexts[0]
        except Exception:
            ctx_logger.warning("Could not connect to CDP, launching new browser")
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context()

        page = await context.new_page()
        await page.goto(search_url, timeout=60000)
        
        page_num = 1
        added = 0
        
        while True:
            ctx_logger.info("Scanning PAGE %s", page_num)
            cards = page.locator("a[data-jk]")
            job_data = []
            
            count = await cards.count()
            for i in range(count):
                card = cards.nth(i)
                jk = await card.get_attribute("data-jk")
                href = await card.get_attribute("href")
                if not jk or not href: continue
                
                title = (await card.inner_text()).split("\n")[0].strip()
                url = normalize_url(href)
                
                # Basic remote check on card
                card_text = await card.inner_text()
                if is_remote(card_text):
                    job_data.append({"url": url, "title": title})

            for job in job_data:
                res = await db.upsert_job(job["url"], title=job["title"])
                if res.success and res.attempts == 0: # New insert
                    added += 1
                    ctx_logger.info("Saved: %s", job["title"])
            
            next_btn = page.locator("a[aria-label='Next Page']")
            if await next_btn.count() == 0:
                break
            
            await next_btn.first.click()
            await asyncio.sleep(random.uniform(2, 4))
            page_num += 1
            
        await page.close()
        ctx_logger.info("Scrape complete. Added %s new jobs.", added)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scrape_jobs.py <indeed_search_url>")
        sys.exit(1)
    asyncio.run(scrape(sys.argv[1]))
