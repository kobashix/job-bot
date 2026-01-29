"""Form filling helpers for internal and external application flows."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from helpers.utils import LocatorCache, UserProfile


@dataclass
class AnswersStore:
    """Stores and retrieves learned answers from disk."""

    path: Path
    data: Dict[str, Dict[str, object]]

    @classmethod
    def load(cls, path: Path) -> "AnswersStore":
        if not path.exists():
            path.write_text("{}")
        return cls(path=path, data=json.loads(path.read_text()))

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True))

    def record(self, label: str, value: str) -> None:
        key = normalize_answer_key(label)
        if not key:
            return
        entry = self.data.get(key, {"default": value, "aliases": []})
        entry["default"] = value
        aliases = set(entry.get("aliases", []))
        aliases.add(label)
        entry["aliases"] = sorted(aliases)
        self.data[key] = entry
        self.save()


def normalize_answer_key(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in text.lower())
    return "_".join(cleaned.split())[:80]


class FormFiller:
    """Handles autofill logic for application forms."""

    COMMUTE_KEYWORDS = ["commute", "commuting", "distance", "travel", "onsite", "on-site", "relocation", "relocate"]
    VOLUNTARY_KEYWORDS = ["voluntary", "self-identification", "self identification", "eeo"]
    DEFAULT_EEO = {
        "gender": "Male",
        "ethnicity": "Not Hispanic or Latino",
        "race": "White",
        "veteran": "No",
        "disability": "No, I do not have a disability",
    }

    def __init__(
        self,
        profile: UserProfile,
        answers: AnswersStore,
        logger,
        train_mode: bool,
    ) -> None:
        self.profile = profile
        self.answers = answers
        self.logger = logger
        self.train_mode = train_mode
        self.cache = LocatorCache()

    async def handle_resume_screen(self, page) -> None:
        resume_banner = self.cache.get(page, "text=Add a resume for the employer")
        resume_card = self.cache.get(page, "text=Use your Indeed Resume")
        upload_option = self.cache.get(page, "text=Upload a resume")
        if not (await resume_banner.count() or await resume_card.count() or await upload_option.count()):
            return
        self.logger.info("Waiting for resume options")
        await self._wait_for_resume_options(page, timeout_s=15)
        selected = await self._ensure_first_resume_selected(page)
        if selected:
            self.logger.info("Resume option selected or already checked")
        await self._click_continue(page, min_wait_s=10, max_wait_s=15)
        await page.wait_for_load_state("networkidle")

    async def handle_inputs(self, page) -> None:
        inputs = self.cache.get(page, "input[type='text'], textarea")
        for i in range(await inputs.count()):
            el = inputs.nth(i)
            context = await extract_context(el)
            try:
                if "name" in context and any(k in context for k in self.VOLUNTARY_KEYWORDS):
                    await el.scroll_into_view_if_needed()
                    await el.fill("Andrew Pennington")
                    self.logger.info("Filled voluntary self-identification name")
                    self._record("voluntary name", "Andrew Pennington")
                elif "your name" in context or "full name" in context or "name" in context:
                    await el.scroll_into_view_if_needed()
                    await el.fill(self.profile.full_name)
                    self.logger.info("Filled name")
                    self._record("full name", self.profile.full_name)
                elif "what industries have you supported" in context:
                    await el.scroll_into_view_if_needed()
                    await el.fill(self.profile.industries_supported)
                    self.logger.info("Filled industries supported")
                    self._record("what industries have you supported", self.profile.industries_supported)
                elif "how large was the organization you supported" in context:
                    await el.scroll_into_view_if_needed()
                    await el.fill(self.profile.organization_size)
                    self.logger.info("Filled organization size")
                    self._record("organization size", self.profile.organization_size)
                elif "size of team managed" in context:
                    await el.scroll_into_view_if_needed()
                    await el.fill(self.profile.team_size)
                    self.logger.info("Filled team size")
                    self._record("team size", self.profile.team_size)
                elif "do you have" in context and "degree" in context:
                    await el.scroll_into_view_if_needed()
                    await el.fill(self.profile.degree_response)
                    self.logger.info("Filled degree response")
                    self._record("degree response", self.profile.degree_response)
                elif "compensation" in context:
                    await el.scroll_into_view_if_needed()
                    await el.fill(self.profile.compensation_range)
                    self.logger.info("Filled compensation range")
                    self._record("compensation", self.profile.compensation_range)
                elif "today" in context or "date" in context:
                    today = datetime.now().strftime("%m/%d/%Y")
                    await el.scroll_into_view_if_needed()
                    await el.fill(today)
                    self.logger.info("Filled date")
                    self._record("today date", today)
                elif "how many years" in context or "years of" in context:
                    await el.scroll_into_view_if_needed()
                    await el.fill("15")
                    self.logger.info("Filled years")
                    self._record("how many years", "15")
            except Exception as exc:
                self.logger.warning("Input fill failed: %s", exc)

    async def handle_radios(self, page) -> None:
        radios = self.cache.get(page, "input[type='radio']")
        total = await radios.count()
        groups: Dict[str, list] = {}
        for i in range(total):
            radio = radios.nth(i)
            try:
                name = await radio.get_attribute("name") or f"_unnamed_{i}"
                groups.setdefault(name, []).append(radio)
            except Exception as exc:
                self.logger.warning("Radio group detection failed: %s", exc)
        for name, group in groups.items():
            if await self._group_has_checked(group):
                continue
            group_text = await _fieldset_text(group[0])
            selected = None
            if any(k in group_text for k in self.COMMUTE_KEYWORDS):
                selected = await self._pick_no_option(group)
            if selected is None:
                selected = await self._pick_first_visible(group)
            if selected is None:
                continue
            label_text = await _label_text(selected)
            try:
                await selected.scroll_into_view_if_needed()
                await selected.check(force=True)
                self.logger.info("Selected radio for group %s", name)
                if label_text:
                    self._record(name, label_text)
            except Exception as exc:
                self.logger.warning("Radio default failed for group %s: %s", name, exc)

    async def handle_special_radios(self, page) -> None:
        try:
            body = (await page.inner_text("body")).lower()
        except Exception:
            body = ""
        merged = {**self.DEFAULT_EEO, **self.profile.eeo}
        for key, val in merged.items():
            if key in body:
                loc = self.cache.get(page, f"label:has-text('{val}')")
                if await loc.count():
                    try:
                        await loc.first.scroll_into_view_if_needed()
                        await loc.first.click(force=True)
                        self.logger.info("Selected EEO %s", val)
                        self._record(key, val)
                    except Exception as exc:
                        self.logger.warning("EEO selection failed for %s: %s", val, exc)
                else:
                    radios = self.cache.get(page, "input[type='radio']")
                    if await radios.count():
                        try:
                            await radios.first.scroll_into_view_if_needed()
                            await radios.first.check(force=True)
                            self.logger.info("Fallback EEO selection for %s", key)
                        except Exception as exc:
                            self.logger.warning("Fallback EEO selection failed for %s: %s", key, exc)

    async def handle_distance_questions(self, page) -> None:
        keywords = self.COMMUTE_KEYWORDS
        try:
            body = (await page.inner_text("body")).lower()
        except Exception:
            body = ""
        if not any(k in body for k in keywords):
            return
        self.logger.info("Detected commuting question")
        radios = self.cache.get(page, "input[type='radio']")
        for i in range(await radios.count()):
            radio = radios.nth(i)
            label_text = await _label_text(radio)
            if "no" in label_text.lower():
                try:
                    await radio.scroll_into_view_if_needed()
                    await radio.check(force=True)
                    self.logger.info("Selected commute radio: %s", label_text)
                    return
                except Exception as exc:
                    self.logger.warning("Distance radio select failed: %s", exc)
        selects = self.cache.get(page, "select")
        if await selects.count():
            try:
                await selects.first.scroll_into_view_if_needed()
                await selects.first.select_option(label="No")
                self.logger.info("Selected commute dropdown: No")
                return
            except Exception as exc:
                self.logger.warning("Distance dropdown select failed: %s", exc)
        inputs = self.cache.get(page, "input[type='text'], textarea")
        for i in range(await inputs.count()):
            el = inputs.nth(i)
            context = await extract_context(el)
            if any(k in context for k in keywords):
                try:
                    await el.scroll_into_view_if_needed()
                    await el.fill("No")
                    self.logger.info("Filled commute input: No")
                    return
                except Exception as exc:
                    self.logger.warning("Distance input fill failed: %s", exc)

    async def handle_relevant_experience(self, page) -> None:
        option_text = "Controller National Park College"
        loc = self.cache.get(page, f"text={option_text}")
        if not await loc.count():
            return
        try:
            await loc.first.scroll_into_view_if_needed()
            await loc.first.click(force=True)
            self.logger.info("Selected experience %s", option_text)
            self._record("relevant_experience", option_text)
        except Exception as exc:
            self.logger.warning("Experience selection failed: %s", exc)

    async def external_fill_inputs(self, page) -> None:
        inputs = self.cache.get(page, "input[type='text'], input[type='email'], input[type='tel'], textarea")
        for i in range(await inputs.count()):
            el = inputs.nth(i)
            context = await extract_context(el)
            try:
                if "name" in context and any(k in context for k in self.VOLUNTARY_KEYWORDS):
                    await el.scroll_into_view_if_needed()
                    await el.fill("Andrew Pennington")
                    continue
                if "first name" in context:
                    await el.scroll_into_view_if_needed()
                    await el.fill(self.profile.first_name)
                elif "last name" in context:
                    await el.scroll_into_view_if_needed()
                    await el.fill(self.profile.last_name)
                elif "full name" in context or "your name" in context:
                    await el.scroll_into_view_if_needed()
                    await el.fill(self.profile.full_name)
                elif "email" in context and self.profile.email:
                    await el.scroll_into_view_if_needed()
                    await el.fill(self.profile.email)
                elif "phone" in context and self.profile.phone:
                    await el.scroll_into_view_if_needed()
                    await el.fill(self.profile.phone)
                elif "how many years" in context:
                    await el.scroll_into_view_if_needed()
                    await el.fill("15")
                elif "today" in context or "date" in context:
                    today = datetime.now().strftime("%m/%d/%Y")
                    await el.scroll_into_view_if_needed()
                    await el.fill(today)
            except Exception as exc:
                self.logger.warning("External input fill failed: %s", exc)

    async def external_select_dropdowns(self, page) -> None:
        selects = self.cache.get(page, "select")
        for i in range(await selects.count()):
            select = selects.nth(i)
            try:
                options = select.locator("option")
                chosen: Optional[str] = None
                for j in range(await options.count()):
                    opt = options.nth(j)
                    value = await opt.get_attribute("value")
                    text = (await opt.inner_text()).strip()
                    if not value or "select" in text.lower() or "choose" in text.lower():
                        continue
                    chosen = value
                    break
                if chosen:
                    await select.scroll_into_view_if_needed()
                    await select.select_option(value=chosen)
                    self.logger.info("Selected external dropdown option: %s", chosen)
            except Exception as exc:
                self.logger.warning("External dropdown select failed: %s", exc)

    async def external_handle_radios(self, page) -> None:
        keywords_no = [*self.COMMUTE_KEYWORDS, "sponsorship", "visa"]
        radios = self.cache.get(page, "input[type='radio']")
        total = await radios.count()
        groups: Dict[str, list] = {}
        for i in range(total):
            radio = radios.nth(i)
            try:
                name = await radio.get_attribute("name") or f"_unnamed_{i}"
                groups.setdefault(name, []).append(radio)
            except Exception as exc:
                self.logger.warning("External radio group detection failed: %s", exc)
        for name, group in groups.items():
            if await self._group_has_checked(group):
                continue
            group_text = await _fieldset_text(group[0])
            prefer_no = any(k in group_text for k in keywords_no)
            selected = None
            if prefer_no:
                selected = await self._pick_no_option(group)
            if selected is None:
                selected = await self._pick_first_visible(group)
            if selected is None:
                continue
            label_text = await _label_text(selected)
            try:
                await selected.scroll_into_view_if_needed()
                await selected.check(force=True)
                self.logger.info("Selected external radio for group %s", name)
            except Exception as exc:
                self.logger.warning("External radio selection failed for group %s: %s", name, exc)

    async def external_handle_demographics(self, page) -> None:
        keywords = ["gender", "ethnicity", "race", "veteran", "disability"]
        try:
            body = (await page.inner_text("body")).lower()
        except Exception:
            body = ""
        if not any(k in body for k in keywords):
            return
        merged = {**self.DEFAULT_EEO, **self.profile.eeo}
        for key, val in merged.items():
            if key in body:
                loc = self.cache.get(page, f"label:has-text('{val}')")
                if await loc.count():
                    try:
                        await loc.first.scroll_into_view_if_needed()
                        await loc.first.click(force=True)
                        self.logger.info("External demographic selected %s", val)
                    except Exception as exc:
                        self.logger.warning("External demographic selection failed for %s: %s", val, exc)

    async def _group_has_checked(self, group) -> bool:
        for radio in group:
            try:
                if await radio.is_checked():
                    return True
            except Exception:
                continue
        return False

    async def _pick_first_visible(self, group):
        for radio in group:
            try:
                if await radio.is_visible() and await radio.is_enabled():
                    return radio
            except Exception:
                continue
        return None

    async def _pick_no_option(self, group):
        for radio in group:
            label_text = await _label_text(radio)
            if "no" in label_text.lower():
                try:
                    if await radio.is_enabled():
                        return radio
                except Exception:
                    return radio
        return None

    def _record(self, label: str, value: str) -> None:
        if self.train_mode:
            self.answers.record(label, value)

    async def _wait_for_resume_options(self, page, timeout_s: int) -> None:
        start = datetime.now().timestamp()
        while datetime.now().timestamp() - start < timeout_s:
            resume_choices = page.locator(
                "[role='radio'], input[type='radio'], text=Use your Indeed Resume, text=Upload a resume"
            )
            if await resume_choices.count():
                return
            await page.wait_for_timeout(500)

    async def _ensure_first_resume_selected(self, page) -> bool:
        cards = page.locator("[role='radio'], input[type='radio']")
        if not await cards.count():
            cards = self.cache.get(page, "text=Use your Indeed Resume")
        if not await cards.count():
            self.logger.warning("Resume options not found on resume screen")
            return False
        card = cards.first
        try:
            selected = (
                await card.locator("[aria-checked='true'], [data-checked='true'], [data-selected='true']").count()
            ) > 0
        except Exception as exc:
            self.logger.warning("Resume selection check failed: %s", exc)
            selected = False
        if selected:
            return True
        try:
            await card.scroll_into_view_if_needed()
            await card.click(force=True)
            return True
        except Exception as exc:
            self.logger.warning("Resume card click failed: %s", exc)
            return False

    async def _click_continue(self, page, min_wait_s: int, max_wait_s: int) -> None:
        start = datetime.now().timestamp()
        last_error = None
        while datetime.now().timestamp() - start < max_wait_s:
            if datetime.now().timestamp() - start < min_wait_s:
                await page.wait_for_timeout(1000)
                continue
            candidates = [
                page.get_by_role("button", name="Continue"),
                page.locator("button:has-text('Continue')"),
                page.locator("a:has-text('Continue')"),
            ]
            for button in candidates:
                if not await button.count():
                    continue
                try:
                    await button.first.scroll_into_view_if_needed()
                    await button.first.click(timeout=15000)
                    self.logger.info("Clicked Continue on resume screen")
                    return
                except Exception as exc:
                    last_error = exc
                    self.logger.warning("Resume Continue click failed: %s", exc)
            await page.wait_for_timeout(1000)
        if last_error:
            self.logger.warning("Resume Continue click failed after retries: %s", last_error)


async def extract_context(el) -> str:
    try:
        label = await el.evaluate(
            """e => {
                const label = e.closest('label') || document.querySelector(`label[for='${e.id}']`);
                const aria = e.getAttribute('aria-label') || e.getAttribute('placeholder') || '';
                const parentText = e.closest('div')?.innerText || '';
                return [label?.innerText || '', aria, parentText].join(' ').toLowerCase();
            }"""
        )
    except Exception:
        label = ""
    return label


async def _label_text(radio) -> str:
    try:
        label = await radio.evaluate(
            """e => {
                const label = e.closest('label') || document.querySelector(`label[for='${e.id}']`);
                return label ? label.innerText.trim() : '';
            }"""
        )
    except Exception:
        label = ""
    return label


async def _fieldset_text(radio) -> str:
    try:
        text = await radio.evaluate("e => e.closest('fieldset')?.innerText?.toLowerCase() || ''")
    except Exception:
        text = ""
    return text
