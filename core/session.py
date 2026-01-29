"""Core session driver for job application automation."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from playwright.async_api import TimeoutError as PWTimeout, async_playwright

from helpers.captcha import CaptchaDetected, CaptchaHandler
from helpers.db import DBClient
from helpers.form_fill import FormFiller

SUCCESS_TEXTS = [
    "application has been submitted",
    "your application has been submitted",
    "thank you for applying",
    "application submitted",
    "thank you",
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

EXTERNAL_BUTTON_LABELS = ["Apply on company site", "Apply externally"]
EXTERNAL_ACTION_LABELS = ["Apply", "Next", "Continue"]
EXTERNAL_FINAL_SUBMIT_LABELS = ["Submit application", "Finish", "Complete application"]


class NavigationTimeout(Exception):
    """Raised when navigation times out."""


class InvalidJobPage(Exception):
    """Raised when job page is invalid or expired."""


@dataclass
class SessionExit(Exception):
    """Raised to stop session with an exit code."""

    code: int
    status: str
    reason: Optional[str] = None


@dataclass
class ProgressMarker:
    url: Optional[str] = None
    value: Optional[str] = None
    timestamp: Optional[float] = None


class PlaywrightSession:
    """Coordinates the application flow using Playwright."""

    def __init__(
        self,
        job_url: str,
        train_mode: bool,
        config,
        db: DBClient,
        captcha: CaptchaHandler,
        form_filler: FormFiller,
        logger,
    ) -> None:
        self.job_url = job_url
        self.train_mode = train_mode
        self.config = config
        self.db = db
        self.captcha = captcha
        self.form_filler = form_filler
        self.logger = logger
        self.progress = ProgressMarker()
        self.last_action = {"label": None, "fn": None}
        self.ats = "indeed"
        self.checkpoints: list[str] = []

    async def run(self) -> None:
        """Run the main application flow."""

        async with async_playwright() as playwright:
            self.logger.info("Connecting to Chromium", extra=self._log_ctx("init"))
            browser = await playwright.chromium.connect_over_cdp(self.config.cdp_url)
            context = browser.contexts[0]
            page = await context.new_page()
            await self._navigate(page)
            await self._handle_landing(page, context)
            await self._apply_loop(page, context)

    async def _navigate(self, page) -> None:
        self.logger.info("Navigating to job page", extra=self._log_ctx("navigate"))
        try:
            response = await page.goto(self.job_url, timeout=60000)
        except PWTimeout as exc:
            await self.db.update_job(self.job_url, "failed", "navigation_timeout")
            raise NavigationTimeout(str(exc))
        await page.wait_for_load_state("networkidle")
        await self._detect_invalid(page, response)
        await self._detect_not_found_and_delete(page)
        await self._update_job_metadata(page)
        await self._handle_additional_verification(page)
        await self._detect_non_remote_job(page)
        if await self._detect_already_applied(page):
            raise SessionExit(0, "already_applied")
        await self._detect_captcha(page, "landing")

    async def _handle_landing(self, page, context) -> None:
        external_info = await self._detect_external_context(page, context)
        if external_info:
            await self._external_apply_handler(page, context, external_info)
            raise SessionExit(0, "external")
        if not await self._find_apply_button(page):
            await self.db.update_job(self.job_url, "no_apply", "missing_apply_button")
            raise SessionExit(10, "no_apply")

    async def _apply_loop(self, page, context) -> None:
        for step in range(self.config.max_steps):
            self._record_checkpoint(f"step_{step + 1}")
            self.logger.info("Step %s", step + 1, extra=self._log_ctx(f"step_{step + 1}"))
            await page.wait_for_load_state("networkidle")
            await self._detect_invalid(page, None)
            await self._detect_not_found_and_delete(page)
            await self._update_job_metadata(page)
            await self._handle_additional_verification(page)
            await self._detect_non_remote_job(page)
            if await self._detect_already_applied(page):
                raise SessionExit(0, "already_applied")
            await self._detect_captcha(page, f"step_{step + 1}")
            external_info = await self._detect_external_context(page, context)
            if external_info:
                await self._external_apply_handler(page, context, external_info)
                raise SessionExit(0, "external")
            await self._check_for_stall(page)

            if await self._is_success_page(page):
                await self.db.update_job(self.job_url, "applied")
                self.logger.info("Application submitted", extra=self._log_ctx("success"))
                await page.close()
                raise SessionExit(0, "applied")

            await self.form_filler.handle_resume_screen(page)
            await self.form_filler.handle_inputs(page)
            await self.form_filler.handle_relevant_experience(page)
            await self.form_filler.handle_distance_questions(page)
            await self.form_filler.handle_radios(page)
            await self.form_filler.handle_special_radios(page)

            if await self._click_any(page, ["Continue", "Review", "Submit", "Submit your application"], timeout=20000):
                await page.wait_for_load_state("networkidle")
                continue

            self.logger.info("No actionable button found", extra=self._log_ctx("wait"))
            await page.wait_for_load_state("networkidle")

        await self.db.update_job(self.job_url, "failed", "max_steps")
        raise SessionExit(99, "max_steps")

    async def _detect_invalid(self, page, response) -> None:
        status = response.status if response is not None else None
        if status and status >= 400:
            if status in {404, 410}:
                await self.db.delete_job(self.job_url)
                raise SessionExit(13, "deleted")
            await self.db.update_job(self.job_url, "invalid", f"http_status_{status}")
            raise InvalidJobPage(f"http_status_{status}")
        try:
            body = (await page.inner_text("body")).lower()
            title = (await page.title()).lower()
        except Exception as exc:
            self.logger.warning("Failed reading page text: %s", exc)
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
                await self.db.update_job(self.job_url, "invalid", f"invalid_text:{text}")
                raise InvalidJobPage(text)

    async def _detect_not_found_and_delete(self, page) -> None:
        try:
            body = (await page.inner_text("body")).lower()
            title = (await page.title()).lower()
        except Exception as exc:
            self.logger.warning("Failed reading page text: %s", exc)
            return
        if (
            "this job has expired on indeed" in body
            or "this job has expired on indeed" in title
            or "job has expired on indeed" in body
            or "job expired on indeed" in body
        ):
            await self.db.delete_job(self.job_url)
            raise SessionExit(13, "deleted")
        if "not found" in body or "not found" in title or "404" in title or "404" in body:
            await self.db.delete_job(self.job_url)
            raise SessionExit(13, "deleted")

    async def _detect_non_remote_job(self, page) -> None:
        selectors = [
            "[data-testid*='location']",
            ".jobsearch-JobInfoHeader-subtitle",
            ".jobsearch-JobInfoHeader-subtitle div",
            "span:has-text('Location')",
        ]
        location_candidates = []
        for selector in selectors:
            loc = page.locator(selector)
            if await loc.count():
                try:
                    text = (await loc.first.inner_text()).strip()
                except Exception as exc:
                    self.logger.warning("Location read failed: %s", exc)
                    continue
                if text:
                    location_candidates.append(text)
        for text in location_candidates:
            if "remote" in text.lower():
                return
        if location_candidates:
            await self.db.update_job(self.job_url, "non_remote", f"non_remote:{location_candidates[0][:200]}")
            raise SessionExit(14, "non_remote")

    async def _detect_already_applied(self, page) -> bool:
        try:
            applied_button = page.get_by_role("button", name="Applied")
            if await applied_button.count() and await applied_button.first.is_visible():
                await self.db.update_job(self.job_url, "applied", "already_applied")
                self.logger.info("Already applied badge detected", extra=self._log_ctx("already_applied"))
                return True
        except Exception:
            pass

        applied_locators = [
            "button[aria-label='Applied']",
            "button:has-text('Applied')",
            "span:has-text('Applied')",
            "div:has-text('Applied')",
            "[data-testid*='applied' i]",
            "[aria-label*='applied' i]",
            "[aria-pressed='true']:has-text('Applied')",
            "text=/\\bApplied\\b/i",
        ]
        for selector in applied_locators:
            locator = page.locator(selector)
            try:
                if await locator.count() and await locator.first.is_visible():
                    await self.db.update_job(self.job_url, "applied", "already_applied")
                    self.logger.info("Already applied badge detected", extra=self._log_ctx("already_applied"))
                    return True
            except Exception:
                continue

        try:
            body = (await page.inner_text("body")).lower()
        except Exception:
            body = ""
        if "applied" in body and "apply" in body:
            await self.db.update_job(self.job_url, "applied", "already_applied_text")
            self.logger.info("Already applied text detected", extra=self._log_ctx("already_applied"))
            return True
        return False

    async def _update_job_metadata(self, page) -> None:
        title = await self._first_text(
            page,
            [
                "h1",
                "[data-testid*='jobTitle' i]",
                ".jobsearch-JobInfoHeader-title",
            ],
        )
        company = await self._first_text(
            page,
            [
                "[data-testid*='companyName' i]",
                ".jobsearch-CompanyInfoContainer a",
                ".jobsearch-CompanyInfoContainer div",
            ],
        )
        location = await self._first_text(
            page,
            [
                "[data-testid*='location' i]",
                ".jobsearch-JobInfoHeader-subtitle div",
                ".jobsearch-JobInfoHeader-subtitle",
            ],
        )
        try:
            await self.db.update_job_metadata(self.job_url, title=title, company=company, location=location)
        except Exception as exc:
            self.logger.warning("Metadata update failed: %s", exc)

    async def _first_text(self, page, selectors: list[str]) -> Optional[str]:
        for selector in selectors:
            locator = page.locator(selector)
            try:
                if await locator.count():
                    text = (await locator.first.inner_text()).strip()
                    if text:
                        return text
            except Exception:
                continue
        return None

    async def _handle_additional_verification(self, page) -> None:
        try:
            body = (await page.inner_text("body")).lower()
            title = (await page.title()).lower()
        except Exception as exc:
            self.logger.warning("Failed reading page text: %s", exc)
            return
        if "additional verification needed" in body or "additional verification needed" in title:
            await self.db.update_job(self.job_url, "captcha_pending", "additional_verification")
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(input, "Additional verification needed. Solve captcha then press ENTER\\n"),
                    timeout=self.config.captcha_wait_seconds,
                )
            except asyncio.TimeoutError:
                await self.db.update_job(self.job_url, "captcha_pending", "verification_timeout")
                raise SessionExit(21, "verification_timeout")
            except EOFError:
                await self.db.update_job(self.job_url, "captcha_pending", "verification_prompt_failed")
                raise SessionExit(21, "verification_prompt_failed")

    async def _detect_captcha(self, page, reason: str) -> None:
        try:
            await self.captcha.detect(page, reason, allow_retry=True, retry_callback=self._retry_last_action)
        except CaptchaDetected as exc:
            await self.db.update_job(
                self.job_url,
                "captcha_pending",
                exc.reason,
                is_external=exc.is_external,
            )
            await self.captcha.resolve(exc.reason)
            await self._retry_last_action()

    async def _retry_last_action(self) -> None:
        if self.last_action["fn"] is None:
            return
        self.logger.info("Retrying last action: %s", self.last_action["label"])
        try:
            await self.last_action["fn"]()
        except Exception as exc:
            self.logger.warning("Retry action failed: %s", exc)

    async def _find_apply_button(self, page) -> bool:
        return await self._click_any(
            page,
            ["Apply", "Apply now", "Apply on company site"],
            timeout=15000,
            allow_links=True,
        )

    async def _click_any(self, page, labels, timeout: int, allow_links: bool = False) -> bool:
        for label in labels:
            locators = [page.locator(f"button:has-text('{label}')")]
            try:
                locators.insert(0, page.get_by_role("button", name=label))
            except Exception:
                pass
            if allow_links:
                try:
                    locators.append(page.get_by_role("link", name=label))
                except Exception:
                    pass
                locators.append(page.locator(f"a:has-text('{label}')"))
            for loc in locators:
                try:
                    if await loc.count() == 0:
                        continue
                    if self.train_mode and "submit" in label.lower():
                        self.logger.info("Training mode: skipping submit click")
                        return False
                    await loc.first.scroll_into_view_if_needed()
                    self.last_action["label"] = label
                    self.last_action["fn"] = lambda l=loc.first, t=timeout: l.click(timeout=t, force=True)
                    if "submit" in label.lower():
                        await page.wait_for_load_state("networkidle")
                    await loc.first.click(timeout=timeout, force=True)
                    self.logger.info("Clicked %s", label)
                    return True
                except Exception as exc:
                    self.logger.warning("Click failed on %s: %s", label, exc)
        return False

    async def _detect_external_context(self, page, context) -> Optional[dict]:
        current_url = page.url
        host = urlparse(current_url).netloc.lower()
        if host and not self._is_indeed_host(host):
            return {"page": page, "final_url": current_url, "reason": "host_change"}
        for label in EXTERNAL_BUTTON_LABELS:
            button = page.locator(f"button:has-text('{label}')")
            if await button.count():
                return {"page": page, "final_url": current_url, "reason": f"button:{label}", "button": label}
        for extra_page in context.pages:
            try:
                extra_url = extra_page.url
            except Exception:
                continue
            extra_host = urlparse(extra_url).netloc.lower()
            if extra_host and not self._is_indeed_host(extra_host):
                return {"page": extra_page, "final_url": extra_url, "reason": "new_tab"}
        if any(k in host for k in EXTERNAL_HOST_KEYWORDS):
            return {"page": page, "final_url": current_url, "reason": "keyword_host"}
        return None

    async def _external_apply_handler(self, page, context, external_info: dict) -> None:
        active_page = external_info["page"]
        if external_info.get("button"):
            label = external_info["button"]
            self.logger.info("External triggered by button: %s", label)
            button = active_page.locator(f"button:has-text('{label}')")
            try:
                await button.first.scroll_into_view_if_needed()
                await button.first.click(timeout=15000)
            except Exception as exc:
                self.logger.warning("External apply button click failed: %s", exc)
            try:
                active_page = await context.wait_for_event("page", timeout=15000)
            except Exception as exc:
                self.logger.warning("No new tab detected after external apply button: %s", exc)
        final_url = active_page.url
        ats = await self._detect_ats(final_url, active_page)
        self.ats = ats
        await self.db.update_job(
            self.job_url,
            "external",
            self._format_external_reason(final_url, ats, external_info.get("reason", "external_detected")),
            is_external=True,
        )
        for step in range(self.config.max_steps):
            self._record_checkpoint(f"external_step_{step + 1}")
            self.logger.info(
                "External step %s", step + 1, extra=self._log_ctx(f"external_step_{step + 1}")
            )
            await active_page.wait_for_load_state("networkidle")
            await self._detect_captcha(active_page, f"external_step_{step + 1}")
            await self.form_filler.external_fill_inputs(active_page)
            await self.form_filler.external_select_dropdowns(active_page)
            await self.form_filler.external_handle_radios(active_page)
            await self.form_filler.external_handle_demographics(active_page)

            submit_result = await self._external_handle_submit(active_page)
            if submit_result == "confirmation_unavailable":
                await self._external_fail("navigation_blocked", "confirmation_unavailable", active_page, final_url, ats)
            if submit_result == "navigation_blocked":
                await self._external_fail("navigation_blocked", "submit_blocked", active_page, final_url, ats)
            if submit_result == "skipped":
                self.logger.info("Training mode: skipping external submit", extra=self._log_ctx("train_skip"))
                continue
            if submit_result == "submitted":
                await active_page.wait_for_load_state("networkidle")
                if await self._is_success_page(active_page):
                    await self.db.update_job(
                        self.job_url,
                        "applied",
                        self._format_external_reason(active_page.url, ats, "external_applied"),
                        is_external=True,
                    )
                    await active_page.close()
                    raise SessionExit(0, "applied")
                await self.db.update_job(
                    self.job_url,
                    "external_submitted",
                    self._format_external_reason(active_page.url, ats, "submitted_no_confirmation"),
                    is_external=True,
                )
                await active_page.close()
                raise SessionExit(0, "external_submitted")

            if await self._external_click_actions(active_page):
                continue

            if await self._external_detect_required_errors(active_page):
                await self._external_fail("missing_required_fields", "required_fields", active_page, final_url, ats)

            self.logger.info("External flow: no actionable button")
            await asyncio.sleep(2)

        await self._external_fail("unsupported_external_flow", "max_steps", active_page, final_url, ats)

    async def _external_handle_submit(self, page) -> Optional[str]:
        for label in EXTERNAL_FINAL_SUBMIT_LABELS:
            loc = page.locator(f"button:has-text('{label}')")
            if await loc.count():
                if self.train_mode:
                    return "skipped"
                if not await self._external_confirm_submit():
                    return "confirmation_unavailable"
                try:
                    await loc.first.scroll_into_view_if_needed()
                    await loc.first.click(timeout=20000)
                    return "submitted"
                except Exception:
                    return "navigation_blocked"
        return None

    async def _external_confirm_submit(self) -> bool:
        if self.train_mode:
            self.logger.info("Training mode: skipping external submit confirmation")
            return False
        try:
            await asyncio.wait_for(
                asyncio.to_thread(input, "READY TO SUBMIT EXTERNAL APPLICATION – press ENTER to continue\n"),
                timeout=self.config.captcha_wait_seconds,
            )
            return True
        except Exception:
            self.logger.warning("Submission confirmation unavailable")
            return False

    async def _external_click_actions(self, page) -> bool:
        for label in EXTERNAL_ACTION_LABELS:
            loc = page.locator(f"button:has-text('{label}')")
            if await loc.count():
                try:
                    await loc.first.scroll_into_view_if_needed()
                    await loc.first.click(timeout=15000)
                    self.logger.info("External click %s", label)
                    return True
                except Exception as exc:
                    self.logger.warning("External click failed on %s: %s", label, exc)
        return False

    async def _external_detect_required_errors(self, page) -> bool:
        try:
            body = (await page.inner_text("body")).lower()
        except Exception as exc:
            self.logger.warning("External required check failed: %s", exc)
            return False
        return "required" in body or "please fill" in body or "missing" in body

    async def _external_fail(self, status: str, reason: str, page, final_url: str, ats: str) -> None:
        await self._external_capture_screenshot(page, reason)
        await self.db.update_job(
            self.job_url,
            status,
            self._format_external_reason(final_url, ats, reason),
            is_external=True,
        )
        raise SessionExit(30, status, reason)

    async def _external_capture_screenshot(self, page, reason: str) -> None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = self.config.artifact_dir / f"external_failure_{timestamp}.png"
        self.config.artifact_dir.mkdir(parents=True, exist_ok=True)
        try:
            await page.screenshot(path=str(filename), full_page=True)
            self.logger.info("External screenshot saved: %s (%s)", filename, reason)
        except Exception as exc:
            self.logger.warning("Screenshot capture failed: %s", exc)

    async def _update_progress_marker(self, page) -> None:
        url = page.url
        value = None
        progress = page.locator("[role='progressbar'], progress, [aria-valuenow]")
        if await progress.count():
            value = await progress.first.get_attribute("aria-valuenow") or await progress.first.get_attribute(
                "value"
            )
        self.progress = ProgressMarker(url=url, value=value, timestamp=asyncio.get_event_loop().time())

    async def _check_for_stall(self, page, threshold: int = 20) -> None:
        if self.progress.timestamp is None:
            await self._update_progress_marker(page)
            return
        now = asyncio.get_event_loop().time()
        url = page.url
        value = None
        progress = page.locator("[role='progressbar'], progress, [aria-valuenow]")
        if await progress.count():
            value = await progress.first.get_attribute("aria-valuenow") or await progress.first.get_attribute(
                "value"
            )
        if url == self.progress.url and value == self.progress.value and now - self.progress.timestamp > threshold:
            self.logger.info("No progress detected, rescanning for actions")
            await self._detect_captcha(page, "stall_detected")
            await self._click_any(page, ["Continue", "Review", "Submit", "Submit your application"], timeout=8000)
            await self._update_progress_marker(page)
        elif url != self.progress.url or value != self.progress.value:
            await self._update_progress_marker(page)

    async def _detect_ats(self, final_url: str, page) -> str:
        lower_url = final_url.lower()
        ats_map = {
            "workday": "Workday",
            "greenhouse": "Greenhouse",
            "lever": "Lever",
            "icims": "iCIMS",
            "paycom": "Paycom",
            "adp": "ADP",
            "bamboohr": "BambooHR",
            "taleo": "Taleo",
            "jobvite": "Jobvite",
        }
        for token, name in ats_map.items():
            if token in lower_url:
                return name
        try:
            body = (await page.inner_text("body")).lower()
        except Exception as exc:
            self.logger.warning("ATS detection failed: %s", exc)
            body = ""
        for token, name in ats_map.items():
            if token in body:
                return name
        return "unknown_external"

    async def _is_success_page(self, page) -> bool:
        try:
            body = (await page.inner_text("body")).lower()
            title = (await page.title()).lower()
        except Exception as exc:
            self.logger.warning("Failed reading page text: %s", exc)
            body, title = "", ""
        if any(text in body or text in title for text in SUCCESS_TEXTS):
            return True
        locator = page.locator(
            "text=/application has been submitted|your application has been submitted|application submitted|thank you for applying/i"
        )
        try:
            if await locator.count() and await locator.first.is_visible():
                return True
        except Exception:
            return False
        return False

    def _format_external_reason(self, final_url: str, ats: str, reason: str) -> str:
        ts = datetime.now().isoformat()
        return f"{reason}|ats={ats}|url={final_url}|ts={ts}"

    def _is_indeed_host(self, host: str) -> bool:
        return host.endswith("indeed.com") or host == "smartapply.indeed.com"

    def _log_ctx(self, step: str) -> dict:
        return {"job_url": self.job_url, "ats": self.ats, "step": step}

    def _record_checkpoint(self, label: str) -> None:
        if not self.checkpoints or self.checkpoints[-1] != label:
            self.checkpoints.append(label)
