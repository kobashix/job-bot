from playwright.sync_api import sync_playwright

print("Attaching to Edge via CDP...")

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://localhost:9222")

    contexts = browser.contexts
    if not contexts:
        raise RuntimeError("No browser contexts found. Edge not started correctly.")

    context = contexts[0]

    pages = context.pages
    page = pages[0] if pages else context.new_page()

    page.goto("https://secure.indeed.com/settings", wait_until="domcontentloaded")

    if "account/login" in page.url or "signin" in page.url:
        print("❌ NOT logged in (redirected to login)")
    else:
        print("✅ Logged in (settings page accessible)")


    print("Page title:", page.title())

    if "Sign in" in page.content():
        print("❌ NOT logged in")
    else:
        print("✅ Logged in session detected")

    input("Press ENTER to close (Edge will stay open)")
