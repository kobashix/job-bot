from playwright.sync_api import sync_playwright
from config import CDP_URL

def get_cdp_page():
    """
    Returns (playwright, browser, context, page)
    Caller must close browser? With CDP, you usually keep Chromium open.
    We'll close Playwright handle only.
    """
    p = sync_playwright().start()
    browser = p.chromium.connect_over_cdp(CDP_URL)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.pages[0] if context.pages else context.new_page()
    return p, browser, context, page
