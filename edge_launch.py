from playwright.sync_api import sync_playwright
import time

EDGE_PATH = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PROFILE_DIR = r"C:\Users\Nope\AppData\Local\Microsoft\Edge\User Data\Profile 1"

with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        executable_path=EDGE_PATH,
        headless=False,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--start-maximized",
            "--no-first-run",
            "--no-default-browser-check"
        ],
    )

    page = context.new_page()
    page.goto("https://www.indeed.com", timeout=60000)

    print("Edge launched as Fetch.")
    print("If this worked, you should already be logged in.")

    # keep browser open so you can SEE it
    time.sleep(120)
    context.close()

