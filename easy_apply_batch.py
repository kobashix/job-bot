"""Batch runner for deterministic apply flows across Indeed and external ATS."""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urlparse

from playwright.async_api import TimeoutError as PWTimeout, async_playwright

from helpers.db import DBClient
from helpers.utils import ContextLogger, load_config, setup_logging

TERMINAL_STATUSES = (
    "APPLIED",
    "EXTERNAL_APPLY",
    "NOT_REMOTE",
    "EXPIRED",
    "CAPTCHA_BLOCKED",
)

SUCCESS_TEXT = "text=/application submitted|thank you for applying|your application has been submitted/i"
COMMUTE_KEYWORDS = ["commute", "distance", "travel", "on-site"]

CAPTCHA_SELECTORS = [
    "iframe[src*='recaptcha' i]",
    "iframe[src*='hcaptcha' i]",
    "iframe[src*='captcha' i]",
    "div[class*='captcha' i]",
    "div[id*='captcha' i]",
    "#cf-challenge",
    "[id*='cf-challenge' i]",
    "[class*='cf-challenge' i]",
]

ATS_HOSTS = {
    "myworkdayjobs.com": "workday",
    "greenhouse.io": "greenhouse",
    "lever.co": "lever",
}


@dataclass
class BatchResult:
    success: int = 0
    failed: int = 0


class BatchRunner:
    """Runs apply flows within a single browser session."""

    def __init__(self, db: DBClient, logger, config) -> None:
        self.db = db
        self.logger = logger
        self.config = config
        self.status_counts: dict[str, int] = {}

    async def run(self, limit: int) -> BatchResult:
        jobs = await self.db.fetch_jobs(limit, TERMINAL_STATUSES)
        self.logger.info("Starting batch apply: %s jobs", len(jobs))
        result = BatchResult()

        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(self.config.cdp_url)
            context = browser.contexts[0]

            for idx, (url,) in enumerate(jobs, 1):
                self.logger.info("[%s/%s] Applying to: %s", idx, len(jobs), url)
                status = await self._apply_job(context, url)
                if status == "APPLIED":
                    result.success += 1
                else:
                    result.failed += 1
                if status:
                    self.status_counts[status] = self.status_counts.get(status, 0) + 1

        self.logger.info("Batch complete")
        self.logger.info("Successes: %s", result.success)
        self.logger.info("Failures: %s", result.failed)
        if self.status_counts:
            for status, count in sorted(self.status_counts.items()):
                self.logger.info("Status %s: %s", status, count)
        if result.success == 0 and result.failed == 0:
            self.logger.warning(
                "No jobs processed. Check filters, job status, or database contents."
            )
        return result

    async def _apply_job(self, context, url: str) -> str:
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except PWTimeout:
            await self._record_failure(page, url, "ERROR_TIMEOUT", "TIMEOUT")
            return "ERROR_TIMEOUT"

        if await self._safe_wait_for_page_ready(page, url) is False:
            await self._record_failure(page, url, "ERROR_TIMEOUT", "TIMEOUT")
            return "ERROR_TIMEOUT"

        if await self._detect_and_pause_for_captcha(page, url):
            await self.db.update_job(url, "CAPTCHA_BLOCKED", "CAPTCHA_BLOCKED")

        if await self._detect_already_applied(page):
            await self.db.update_job(url, "APPLIED", "already_applied")
            await page.close()
            return "APPLIED"

        ats_type = self._detect_ats(page.url)
        if ats_type != "indeed":
            status = await self._handle_external_ats(page, url, ats_type)
            if status:
                await page.close()
            return status

        status = await self._handle_indeed_flow(page, url)
        if status:
            await page.close()
        return status

    async def _safe_wait_for_page_ready(self, page, url: str, timeout_ms: int = 30000) -> bool:
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
            return True
        except PWTimeout:
            pass
        actionable = page.locator(
            "input:visible, textarea:visible, select:visible, [role='button']:visible, "
            "button:visible, text=/apply|continue|next|submit/i"
        )
        try:
            await actionable.first.wait_for(state="visible", timeout=timeout_ms)
            return True
        except PWTimeout:
            await self._record_failure(page, url, "ERROR_TIMEOUT", "TIMEOUT")
            return False

    async def _detect_and_pause_for_captcha(self, page, url: str) -> bool:
        for selector in CAPTCHA_SELECTORS:
            locator = page.locator(selector)
            try:
                if await locator.count() and await locator.first.is_visible():
                    self.logger.warning("CAPTCHA_DETECTED at %s", url)
                    print("[CAPTCHA] Solve manually, then press ENTER to continue")
                    await asyncio.to_thread(input)
                    return True
            except Exception:
                continue
        return False

    async def _detect_already_applied(self, page) -> bool:
        locator = page.locator(r"text=/\bApplied\b/i")
        try:
            return await locator.count() and await locator.first.is_visible()
        except Exception:
            return False

    def _detect_ats(self, url: str) -> str:
        host = urlparse(url).netloc.lower()
        for token, name in ATS_HOSTS.items():
            if token in host:
                return name
        if host.endswith("indeed.com") or host == "smartapply.indeed.com":
            return "indeed"
        return "generic"

    async def _handle_indeed_flow(self, page, url: str) -> str:
        apply_state = await self._click_apply_cta(page)
        if apply_state == "external":
            await self.db.update_job(url, "EXTERNAL_APPLY", "company_site", is_external=True)
            return "EXTERNAL_APPLY"

        for step in range(self.config.max_steps):
            if await self._detect_and_pause_for_captcha(page, url):
                await self.db.update_job(url, "CAPTCHA_BLOCKED", "CAPTCHA_BLOCKED")

            if await self._is_success(page):
                await self.db.update_job(url, "APPLIED", "application_submitted")
                return "APPLIED"

            await self._handle_resume_screen(page)
            await self._handle_commute_questions(page)
            await self._handle_radios(page)

            if await self._click_action_button(page):
                await self._safe_wait_for_page_ready(page, url)
                continue

            await self._record_failure(page, url, "ERROR_FORM_INCOMPLETE", "FORM_INCOMPLETE")
            return "ERROR_FORM_INCOMPLETE"

        await self._record_failure(page, url, "ERROR_TIMEOUT", "TIMEOUT")
        return "ERROR_TIMEOUT"

    async def _click_apply_cta(self, page) -> Optional[str]:
        external = page.locator("text=Apply on company site")
        try:
            if await external.count() and await external.first.is_visible():
                return "external"
        except Exception:
            pass

        apply = page.locator("text=Apply now")
        try:
            if await apply.count() and await apply.first.is_visible():
                await apply.first.scroll_into_view_if_needed()
                await apply.first.click(timeout=15000)
                return "easy_apply"
        except Exception:
            return None
        return None

    async def _handle_resume_screen(self, page) -> None:
        banner = page.locator("text=Add a resume for the employer")
        if not await banner.count():
            return
        resume_choice = page.locator("text=Use your Indeed Resume")
        if await resume_choice.count():
            await resume_choice.first.scroll_into_view_if_needed()
            try:
                await resume_choice.first.click(force=True)
            except Exception:
                pass
        continue_btn = page.get_by_role("button", name="Continue")
        if await continue_btn.count():
            try:
                await continue_btn.first.scroll_into_view_if_needed()
                await continue_btn.first.click(timeout=15000)
            except Exception:
                try:
                    await continue_btn.first.click(timeout=15000, force=True)
                except Exception:
                    pass

    async def _handle_commute_questions(self, page) -> None:
        radios = page.locator("input[type='radio']")
        if not await radios.count():
            return
        for idx in range(await radios.count()):
            radio = radios.nth(idx)
            group_text = await self._group_text(radio)
            if group_text and any(k in group_text for k in COMMUTE_KEYWORDS):
                if await self._select_radio_by_label(radio, prefer_no=True):
                    continue

    async def _handle_radios(self, page) -> None:
        radios = page.locator("input[type='radio']")
        total = await radios.count()
        groups: dict[str, list] = {}
        for i in range(total):
            radio = radios.nth(i)
            try:
                name = await radio.get_attribute("name") or f"_unnamed_{i}"
                groups.setdefault(name, []).append(radio)
            except Exception:
                continue
        for group in groups.values():
            if await self._group_has_checked(group):
                continue
            selected = await self._select_radio_by_label(group[0], prefer_no=False)
            if not selected:
                for radio in group:
                    try:
                        if await radio.is_visible() and await radio.is_enabled():
                            await radio.check(force=True)
                            break
                    except Exception:
                        continue

    async def _select_radio_by_label(self, radio, prefer_no: bool) -> bool:
        label_text = await self._label_text(radio)
        if prefer_no and "no" not in label_text.lower():
            return False
        try:
            if await radio.is_visible() and await radio.is_enabled():
                await radio.check(force=True)
                return True
        except Exception:
            return False
        return False

    async def _label_text(self, radio) -> str:
        try:
            return await radio.evaluate(
                """e => {
                const label = e.closest('label') || document.querySelector(`label[for='${e.id}']`);
                return label ? label.innerText.trim() : '';
            }"""
            )
        except Exception:
            return ""

    async def _group_text(self, radio) -> str:
        try:
            return await radio.evaluate("e => e.closest('fieldset')?.innerText?.toLowerCase() || ''")
        except Exception:
            return ""

    async def _group_has_checked(self, group) -> bool:
        for radio in group:
            try:
                if await radio.is_checked():
                    return True
            except Exception:
                continue
        return False

    async def _click_action_button(self, page) -> bool:
        buttons = page.locator("button:visible, [role='button']:visible")
        for idx in range(await buttons.count()):
            button = buttons.nth(idx)
            try:
                text = (await button.inner_text()).strip().lower()
            except Exception:
                text = ""
            if not text or not any(token in text for token in ["apply", "continue", "next", "submit"]):
                continue
            try:
                await button.scroll_into_view_if_needed()
                await button.click(timeout=15000)
                return True
            except Exception:
                try:
                    await button.click(timeout=15000, force=True)
                    return True
                except Exception:
                    continue
        return False

    async def _handle_external_ats(self, page, url: str, ats_type: str) -> str:
        if ats_type == "workday":
            return await self._handle_workday(page, url)
        if ats_type == "greenhouse":
            return await self._handle_greenhouse(page, url)
        if ats_type == "lever":
            return await self._handle_greenhouse(page, url)
        return await self._handle_generic(page, url)

    async def _handle_workday(self, page, url: str) -> str:
        for _ in range(self.config.max_steps):
            if await self._detect_and_pause_for_captcha(page, url):
                await self.db.update_job(url, "CAPTCHA_BLOCKED", "CAPTCHA_BLOCKED")
            if await self._is_success(page):
                await self.db.update_job(url, "APPLIED", "application_submitted", is_external=True)
                return "APPLIED"
            frame = await self._find_best_frame(page)
            target = frame if frame is not None else page
            await self._handle_radios(target)
            if await self._click_action_button(target):
                await self._safe_wait_for_page_ready(page, url)
                continue
            await self._record_failure(page, url, "ERROR_FORM_INCOMPLETE", "FORM_INCOMPLETE")
            return "ERROR_FORM_INCOMPLETE"
        await self._record_failure(page, url, "ERROR_TIMEOUT", "TIMEOUT")
        return "ERROR_TIMEOUT"

    async def _handle_greenhouse(self, page, url: str) -> str:
        if await self._detect_and_pause_for_captcha(page, url):
            await self.db.update_job(url, "CAPTCHA_BLOCKED", "CAPTCHA_BLOCKED")
        await self._handle_radios(page)
        if await self._click_action_button(page):
            await self._safe_wait_for_page_ready(page, url)
        if await self._is_success(page):
            await self.db.update_job(url, "APPLIED", "application_submitted", is_external=True)
            return "APPLIED"
        await self._record_failure(page, url, "ERROR_FORM_INCOMPLETE", "FORM_INCOMPLETE")
        return "ERROR_FORM_INCOMPLETE"

    async def _handle_generic(self, page, url: str) -> str:
        if await self._detect_and_pause_for_captcha(page, url):
            await self.db.update_job(url, "CAPTCHA_BLOCKED", "CAPTCHA_BLOCKED")
        await self._handle_radios(page)
        if await self._click_action_button(page):
            await self._safe_wait_for_page_ready(page, url)
        if await self._is_success(page):
            await self.db.update_job(url, "APPLIED", "application_submitted", is_external=True)
            return "APPLIED"
        await self._record_failure(page, url, "ERROR_ATS_UNSUPPORTED", "ATS_UNSUPPORTED")
        return "ERROR_ATS_UNSUPPORTED"

    async def _find_best_frame(self, page):
        for frame in page.frames:
            if "myworkdayjobs.com" in frame.url:
                return frame
            try:
                if await frame.locator("input, textarea, select, [role='button']").count():
                    return frame
            except Exception:
                continue
        return None

    async def _is_success(self, page) -> bool:
        locator = page.locator(SUCCESS_TEXT)
        try:
            return await locator.count() and await locator.first.is_visible()
        except Exception:
            return False

    async def _record_failure(self, page, url: str, status: str, reason: str) -> None:
        await self._capture_debug(page, reason)
        await self.db.update_job(url, status, reason)

    async def _capture_debug(self, page, reason: str) -> None:
        try:
            await page.screenshot(path=str(self.config.artifact_dir / f"batch_failure_{reason}.png"))
        except Exception:
            pass
        try:
            self.logger.error("Failure URL: %s", page.url)
        except Exception:
            pass
        try:
            buttons = page.locator("button:visible, [role='button']:visible")
            texts = []
            for idx in range(min(await buttons.count(), 10)):
                try:
                    texts.append((await buttons.nth(idx).inner_text()).strip())
                except Exception:
                    continue
            if texts:
                self.logger.error("Visible buttons: %s", ", ".join(texts))
        except Exception:
            pass


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch runner for easy apply")
    parser.add_argument("--limit", type=int, default=10, help="Max jobs to process")
    parser.add_argument("--config", default="config.json", help="Path to config JSON")
    return parser.parse_args(list(argv))


async def run() -> int:
    args = parse_args(sys.argv[1:])
    config = load_config(args.config)
    logger = setup_logging()
    context_logger = ContextLogger(logger, {"job_url": "batch", "ats": "batch", "step": "batch"})
    db_client = DBClient(str(config.db_path), context_logger)
    runner = BatchRunner(db_client, context_logger, config)
    await runner.run(args.limit)
    return 0


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
