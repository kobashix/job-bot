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
FINAL_STATUSES = (
    "APPLIED",
    "EXTERNAL_APPLY",
    "NOT_REMOTE",
    "EXPIRED",
    "CAPTCHA_BLOCKED",
)
CLICK_RETRY_BASE_SECONDS = 0.6
CLICK_RETRY_MAX_SECONDS = 5.0


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
            await self.db.update_job(self.job_url, "ERROR_navigation_timeout", "navigation_timeout")
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
        try:
            await page.wait_for_selector(
                "button[aria-label='Applied'], button:has-text('Apply'), a:has-text('Apply now')",
                timeout=5000,
            )
        except Exception:
            pass
        if await self._detect_already_applied(page):
            raise SessionExit(0, "already_applied")
        apply_state = await self._detect_apply_cta(page)
        if apply_state == "external":
            await self.db.update_job(self.job_url, "EXTERNAL_APPLY", "company_site", is_external=True)
            raise SessionExit(0, "external")
        if apply_state == "easy_apply":
            return
        external_info = await self._detect_external_context(page, context)
        if external_info:
            await self._external_apply_handler(page, context, external_info)
            raise SessionExit(0, "external")
        if not await self._find_apply_button(page):
            self._log_failure("find_apply_button", "missing_apply_button", "landing")
            await self.db.update_job(self.job_url, "ERROR_missing_apply_button", "missing_apply_button")
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
                await self.db.update_job(self.job_url, "APPLIED", "application_submitted")
                self.logger.info("Application submitted", extra=self._log_ctx("success"))
                await page.close()
                raise SessionExit(0, "applied")

            await self.form_filler.handle_resume_screen(page)
            await self.form_filler.handle_inputs(page)
            await self.form_filler.handle_relevant_experience(page)
            await self.form_filler.handle_distance_questions(page)
            await self.form_filler.handle_radios(page)
            await self.form_filler.handle_special_radios(page)
            await asyncio.sleep(0.8)

            if await self._click_any(
                page,
                ["Continue", "Next", "Review", "Submit", "Submit application", "Submit your application"],
                timeout=30000,
            ):
                await asyncio.sleep(1)
                await page.wait_for_load_state("networkidle")
                continue

            self.logger.info(
                "No actionable button found at %s", page.url, extra=self._log_ctx("wait")
            )
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(1)

        self._log_failure("apply_loop", "max_steps", "apply_loop")
        await self.db.update_job(self.job_url, "ERROR_max_steps", "max_steps")
        raise SessionExit(99, "max_steps")

    async def _detect_invalid(self, page, response) -> None:
        status = response.status if response is not None else None
        if status and status >= 400:
            if status in {404, 410}:
                self._log_failure("load_page", f"http_status_{status}", "navigate")
                await self.db.update_job(self.job_url, "EXPIRED", f"http_status_{status}")
                raise InvalidJobPage(f"http_status_{status}")
            self._log_failure("load_page", f"http_status_{status}", "navigate")
            await self.db.update_job(self.job_url, f"ERROR_http_status_{status}", f"http_status_{status}")
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
            "this job has expired",
            "we can't find this page",
            "we can’t find this page",
            "404",
            "page not found",
            "not found",
        ]
        for text in invalid_texts:
            if text in body or text in title:
                status = "EXPIRED" if ("expired" in text or "find this page" in text or "404" in text) else "ERROR_invalid_page"
                self._log_failure("page_validation", f"invalid_text:{text}", "navigate")
                await self.db.update_job(self.job_url, status, f"invalid_text:{text}")
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
            await self.db.update_job(self.job_url, "EXPIRED", "expired_on_indeed")
            self._log_failure("page_validation", "expired_on_indeed", "navigate")
            raise SessionExit(12, "expired")
        if "not found" in body or "not found" in title or "404" in title or "404" in body:
            self._log_failure("page_validation", "not_found", "navigate")
            await self.db.update_job(self.job_url, "EXPIRED", "not_found")
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
        try:
            body = (await page.inner_text("body")).lower()
            title = (await page.title()).lower()
        except Exception as exc:
            self.logger.warning("Failed reading page text for disqualification: %s", exc)
            body, title = "", ""
        non_remote_phrases = [
            "hybrid work",
            "work location: in person",
            "on-site",
            "on site",
            "estimated commute",
        ]
        if any(phrase in body or phrase in title for phrase in non_remote_phrases):
            self._log_failure("disqualification", "non_remote_phrase", "navigate")
            await self.db.update_job(self.job_url, "NOT_REMOTE", "not_remote:phrase_match")
            raise SessionExit(14, "disqualified")
        for text in location_candidates:
            if "remote" in text.lower():
                return
            if "," in text and any(part.strip().isalpha() and len(part.strip()) == 2 for part in text.split(",")):
                self._log_failure("disqualification", "city_state_location", "navigate")
                await self.db.update_job(
                    self.job_url,
                    "NOT_REMOTE",
                    f"not_remote:city_state:{text[:200]}",
                )
                raise SessionExit(14, "disqualified")
        if location_candidates and "remote" not in body:
            self._log_failure("disqualification", "non_remote_location", "navigate")
            await self.db.update_job(
                self.job_url,
                "NOT_REMOTE",
                f"not_remote:location:{location_candidates[0][:200]}",
            )
            raise SessionExit(14, "disqualified")

    async def _detect_already_applied(self, page) -> bool:
        try:
            await page.wait_for_selector(
                "button[aria-label='Applied'], span:has-text('Applied'), div:has-text('Applied')",
                timeout=3000,
                state="visible",
            )
        except Exception:
            pass
        try:
            applied_button = page.get_by_role("button", name="Applied")
            if await applied_button.count() and await applied_button.first.is_visible():
                await self.db.update_job(self.job_url, "APPLIED", "already_applied")
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
                    await self.db.update_job(self.job_url, "APPLIED", "already_applied")
                    self.logger.info("Already applied badge detected", extra=self._log_ctx("already_applied"))
                    return True
            except Exception:
                continue

        try:
            body = (await page.inner_text("body")).lower()
        except Exception:
            body = ""
        if "applied" in body and "apply" in body:
            await self.db.update_job(self.job_url, "APPLIED", "already_applied_text")
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
            await self.db.update_job(self.job_url, "CAPTCHA_BLOCKED", "additional_verification")
            try:
                await asyncio.to_thread(
                    input, "[CAPTCHA] Solve manually, then press ENTER to continue\n"
                )
            except EOFError:
                await self.db.update_job(self.job_url, "CAPTCHA_BLOCKED", "verification_prompt_failed")
                raise SessionExit(21, "verification_prompt_failed")

    async def _detect_captcha(self, page, reason: str) -> None:
        try:
            await self.captcha.detect(page, reason, allow_retry=True, retry_callback=self._retry_last_action)
        except CaptchaDetected as exc:
            await self.db.update_job(
                self.job_url,
                "CAPTCHA_BLOCKED",
                exc.reason,
                is_external=exc.is_external,
            )
            await self.captcha.resolve(exc.reason)
            await self._retry_last_action()

    async def _detect_apply_cta(self, page) -> Optional[str]:
        external_labels = ["Apply on company site"]
        easy_apply_labels = ["Apply now"]
        for label in external_labels:
            for locator in self._text_first_locators(page, label, allow_links=True):
                try:
                    if await locator.count():
                        await locator.first.scroll_into_view_if_needed()
                        if await locator.first.is_visible():
                            self.logger.info("External apply CTA detected: %s", label)
                            return "external"
                except Exception:
                    continue
        for label in easy_apply_labels:
            for locator in self._text_first_locators(page, label, allow_links=True):
                try:
                    if await locator.count():
                        self.last_action["label"] = label
                        self.last_action["fn"] = lambda l=locator.first: self._safe_click(
                            l, label, timeout=30000, allow_force=True
                        )
                        if await self._safe_click(locator.first, label, timeout=30000, allow_force=True):
                            self.logger.info("Clicked %s", label, extra=self._log_ctx("apply"))
                            return "easy_apply"
                except Exception as exc:
                    self.logger.warning("Apply CTA click failed for %s: %s", label, exc)
        return None

    def _text_first_locators(self, page, label: str, allow_links: bool = False) -> list:
        locators = []
        try:
            locators.append(page.get_by_role("button", name=label))
        except Exception:
            pass
        locators.append(page.locator(f"button:has-text('{label}')"))
        if allow_links:
            try:
                locators.append(page.get_by_role("link", name=label))
            except Exception:
                pass
            locators.append(page.locator(f"a:has-text('{label}')"))
        return locators

    async def _retry_last_action(self) -> None:
        if self.last_action["fn"] is None:
            return
        self.logger.info("Retrying last action: %s", self.last_action["label"])
        try:
            await self.last_action["fn"]()
        except Exception as exc:
            self.logger.warning("Retry action failed: %s", exc)

    async def _find_apply_button(self, page) -> bool:
        if await self._click_apply_selector(page):
            return True
        return await self._click_any(
            page,
            ["Apply now", "Apply", "Continue", "Next", "Submit application"],
            timeout=30000,
            allow_links=True,
        )

    async def _click_apply_selector(self, page) -> bool:
        selectors = [
            "[data-testid*='indeedApplyButton' i]",
            "[data-testid*='applyButton' i]",
            "button:has-text('Apply')",
        ]
        for selector in selectors:
            loc = page.locator(selector)
            try:
                if await loc.count() == 0:
                    continue
                self.last_action["label"] = "Apply"
                self.last_action["fn"] = lambda l=loc.first: self._safe_click(
                    l, "Apply", timeout=30000, allow_force=True
                )
                if await self._safe_click(loc.first, "Apply", timeout=30000, allow_force=True):
                    self.logger.info("Clicked apply CTA selector %s", selector)
                    return True
            except Exception as exc:
                self.logger.warning(
                    "Apply CTA click failed for %s at %s: %s", selector, page.url, exc, extra=self._log_ctx("apply")
                )
        return False

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
                    self.last_action["label"] = label
                    self.last_action["fn"] = lambda l=loc.first, t=timeout: self._safe_click(
                        l, label, timeout=t, allow_force=True
                    )
                    if "submit" in label.lower():
                        await page.wait_for_load_state("networkidle")
                    if await self._safe_click(loc.first, label, timeout=timeout, allow_force=True):
                        self.logger.info("Clicked %s", label, extra=self._log_ctx("click"))
                        return True
                except Exception as exc:
                    self.logger.warning(
                        "Click failed on %s at %s (action=click, reason=%s)",
                        label,
                        page.url,
                        exc,
                        extra=self._log_ctx("click"),
                    )
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
        ats_hint = await self._detect_ats(active_page.url, active_page)
        await self.db.update_job(
            self.job_url,
            "EXTERNAL_APPLY",
            self._format_external_reason(
                active_page.url,
                ats_hint,
                external_info.get("reason", "external_detected"),
            ),
            is_external=True,
        )
        if external_info.get("button"):
            label = external_info["button"]
            self.logger.info("External triggered by button: %s", label)
            button = active_page.locator(f"button:has-text('{label}')")
            try:
                await self._safe_click(button.first, label, timeout=15000, allow_force=True)
            except Exception as exc:
                self.logger.warning("External apply button click failed: %s", exc)
            try:
                active_page = await context.wait_for_event("page", timeout=15000)
            except Exception as exc:
                self.logger.warning("No new tab detected after external apply button: %s", exc)
        final_url = active_page.url
        ats = await self._detect_ats(final_url, active_page)
        self.ats = ats
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
                await self._external_fail(
                    "ERROR_confirmation_unavailable", "confirmation_unavailable", active_page, final_url, ats
                )
            if submit_result == "navigation_blocked":
                await self._external_fail("ERROR_submit_blocked", "submit_blocked", active_page, final_url, ats)
            if submit_result == "skipped":
                self.logger.info("Training mode: skipping external submit", extra=self._log_ctx("train_skip"))
                continue
            if submit_result == "submitted":
                await active_page.wait_for_load_state("networkidle")
                if await self._is_success_page(active_page):
                    await self.db.update_job(
                        self.job_url,
                        "APPLIED",
                        self._format_external_reason(active_page.url, ats, "external_applied"),
                        is_external=True,
                    )
                    await active_page.close()
                    raise SessionExit(0, "applied")
                await self.db.update_job(
                    self.job_url,
                    "EXTERNAL_APPLY",
                    self._format_external_reason(active_page.url, ats, "submitted_no_confirmation"),
                    is_external=True,
                )
                await active_page.close()
                raise SessionExit(0, "external_submitted")

            if await self._external_click_actions(active_page):
                continue

            if await self._external_detect_required_errors(active_page):
                await self._external_fail(
                    "ERROR_required_fields", "required_fields", active_page, final_url, ats
                )

            self.logger.info("External flow: no actionable button")
            await asyncio.sleep(2)

        await self._external_fail("ERROR_max_steps", "max_steps", active_page, final_url, ats)

    async def _external_handle_submit(self, page) -> Optional[str]:
        for label in EXTERNAL_FINAL_SUBMIT_LABELS:
            loc = page.locator(f"button:has-text('{label}')")
            if await loc.count():
                if self.train_mode:
                    return "skipped"
                if not await self._external_confirm_submit():
                    return "confirmation_unavailable"
                try:
                    if await self._safe_click(loc.first, label, timeout=20000, allow_force=True):
                        return "submitted"
                    return "navigation_blocked"
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
                    if await self._safe_click(loc.first, label, timeout=15000, allow_force=True):
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
        self._log_failure("external_flow", reason, "external")
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
            await self._click_any(
                page,
                ["Continue", "Next", "Review", "Submit", "Submit application", "Submit your application"],
                timeout=30000,
            )
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

    def _log_failure(self, action: str, reason: str, step: str) -> None:
        self.logger.error(
            "Failure at %s: action=%s reason=%s url=%s",
            step,
            action,
            reason,
            self.job_url,
            extra=self._log_ctx(step),
        )

    async def _safe_click(self, locator, label: str, timeout: int, allow_force: bool) -> bool:
        backoff = CLICK_RETRY_BASE_SECONDS
        for attempt in range(1, 5):
            try:
                if not await locator.count():
                    return False
                target = locator
                if await self._is_disabled(target):
                    self.logger.info(
                        "Skipping click on disabled element: %s", label, extra=self._log_ctx("click")
                    )
                    return False
                try:
                    await target.scroll_into_view_if_needed()
                except Exception:
                    pass
                try:
                    await target.click(timeout=timeout)
                    await asyncio.sleep(0.8)
                    return True
                except Exception as exc:
                    error_text = str(exc).lower()
                    if "not visible" in error_text or "not in viewport" in error_text:
                        self.logger.info(
                            "Element not visible; retrying click on %s (attempt %s)",
                            label,
                            attempt,
                            extra=self._log_ctx("click"),
                        )
                        if allow_force and not await self._is_disabled(target):
                            await target.click(timeout=timeout, force=True)
                            await asyncio.sleep(0.8)
                            return True
                        raise
                    if allow_force and not await self._is_disabled(target):
                        await target.click(timeout=timeout, force=True)
                        await asyncio.sleep(0.8)
                        return True
                    raise
            except Exception as exc:
                self.logger.warning(
                    "Click attempt failed on %s at %s (attempt=%s, reason=%s)",
                    label,
                    self.job_url,
                    attempt,
                    exc,
                    extra=self._log_ctx("click"),
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, CLICK_RETRY_MAX_SECONDS)
        return False

    async def _is_disabled(self, locator) -> bool:
        try:
            if not await locator.is_enabled():
                return True
        except Exception:
            pass
        try:
            disabled = await locator.get_attribute("disabled")
            aria = await locator.get_attribute("aria-disabled")
            if disabled is not None:
                return True
            if aria and aria.lower() in {"true", "disabled"}:
                return True
        except Exception:
            pass
        return False
