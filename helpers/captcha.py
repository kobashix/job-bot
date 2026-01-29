"""Captcha detection and handling helpers."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Iterable, Optional

from helpers.utils import async_input


class CaptchaDetected(Exception):
    """Raised when a captcha challenge is detected."""

    def __init__(self, reason: str, is_external: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.is_external = is_external


@dataclass
class CaptchaConfig:
    """Captcha detection configuration."""

    iframe_selectors: Iterable[str]
    text_locator: str
    challenge_locator: str
    block_texts: Iterable[str]


class CaptchaHandler:
    """Detects captcha challenges and pauses for resolution."""

    def __init__(self, config: CaptchaConfig, logger, wait_seconds: int = 120) -> None:
        self.config = config
        self.logger = logger
        self.wait_seconds = wait_seconds

    async def detect(
        self,
        page,
        reason: str,
        allow_retry: bool,
        retry_callback: Optional[callable],
    ) -> None:
        """Raise CaptchaDetected when a challenge is present."""

        try:
            body = (await page.inner_text("body")).lower()
            title = (await page.title()).lower()
        except Exception as exc:
            self.logger.warning("Failed reading page text: %s", exc)
            body, title = "", ""
        text_locator = page.locator(self.config.text_locator)
        footer_only = "protected by recaptcha" in body and "google privacy policy" in body
        if await self._locator_has_visible(text_locator):
            strong_text = any(
                t in body for t in ["i'm not a robot", "verify you are human", "security check"]
            )
            if footer_only and not strong_text:
                self.logger.info("Footer recaptcha notice detected; no challenge visible")
                return
            await self._retry_or_raise(
                reason=f"{reason}:visible_text",
                allow_retry=allow_retry,
                retry_callback=retry_callback,
            )
        for token in self.config.block_texts:
            if token == "captcha":
                if token in body or token in title:
                    if footer_only and not await page.locator(self.config.challenge_locator).count():
                        self.logger.info("Footer recaptcha notice detected; no challenge visible")
                        return
                    await self._retry_or_raise(
                        reason=f"{reason}:{token}",
                        allow_retry=allow_retry,
                        retry_callback=retry_callback,
                    )
            elif token in body or token in title:
                await self._retry_or_raise(
                    reason=f"{reason}:{token}",
                    allow_retry=allow_retry,
                    retry_callback=retry_callback,
                )
        captcha_frames = page.locator(", ".join(self.config.iframe_selectors))
        if await captcha_frames.count() and await self._locator_has_visible(captcha_frames):
            await self._retry_or_raise(
                reason=f"{reason}:iframe_visible",
                allow_retry=allow_retry,
                retry_callback=retry_callback,
            )
        if await page.locator(self.config.challenge_locator).count():
            await self._retry_or_raise(
                reason=f"{reason}:challenge_frame",
                allow_retry=allow_retry,
                retry_callback=retry_callback,
            )

    async def resolve(self, prompt: str) -> None:
        """Pause for user to resolve captcha."""

        self.logger.warning("Captcha detected: %s", prompt)
        try:
            await async_input("[CAPTCHA] Solve manually, then press ENTER to continue\n")
        except EOFError:
            self.logger.warning("No stdin available to continue")

    async def _retry_or_raise(
        self,
        reason: str,
        allow_retry: bool,
        retry_callback: Optional[callable],
    ) -> None:
        if allow_retry and retry_callback is not None:
            self.logger.info("Captcha indicators detected; retrying action")
            await retry_callback()
            await asyncio.sleep(1)
            raise CaptchaDetected(reason)
        raise CaptchaDetected(reason)

    async def _locator_has_visible(self, locator, limit: int = 5) -> bool:
        try:
            count = min(await locator.count(), limit)
        except Exception:
            return False
        for i in range(count):
            try:
                if await locator.nth(i).is_visible():
                    return True
            except Exception:
                continue
        return False
