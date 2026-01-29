"""Utility helpers for configuration, logging, and async helpers."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv


@dataclass(frozen=True)
class UserProfile:
    """Represents user profile data used in application forms."""

    full_name: str
    first_name: str
    last_name: str
    email: str
    phone: str
    compensation_range: str
    years_experience: str
    industries_supported: str
    organization_size: str
    team_size: str
    degree_response: str
    eeo: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AppConfig:
    """Configuration for the job application session."""

    cdp_url: str
    db_path: Path
    answers_path: Path
    artifact_dir: Path
    max_steps: int
    captcha_wait_seconds: int
    profile: UserProfile


class ContextLogger(logging.LoggerAdapter):
    """LoggerAdapter that injects session context into log records."""

    def process(self, msg: str, kwargs: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        extra = kwargs.setdefault("extra", {})
        extra.update(self.extra)
        return msg, kwargs


class LocatorCache:
    """Caches locator strings per page for reuse."""

    def __init__(self) -> None:
        self._cache: Dict[tuple[int, str], Any] = {}

    def get(self, page: Any, selector: str) -> Any:
        key = (id(page), selector)
        if key not in self._cache:
            self._cache[key] = page.locator(selector)
        return self._cache[key]


async def async_input(prompt: str) -> str:
    """Await user input without blocking the event loop."""

    return await asyncio.to_thread(input, prompt)


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""

    parser = argparse.ArgumentParser(description="Job bot easy apply")
    parser.add_argument("job_url", nargs="?", help="Job URL to apply to")
    parser.add_argument("--train", action="store_true", help="Training mode")
    parser.add_argument("--config", default="config.json", help="Path to config JSON")
    return parser.parse_args()


def setup_logging() -> logging.Logger:
    """Initialize structured logging."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(job_url)s] [%(ats)s] [%(step)s] %(message)s",
    )
    return logging.getLogger("job_bot")


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def load_config(path: Path) -> AppConfig:
    """Load configuration from JSON and environment overrides."""

    load_dotenv()
    data = _load_json(path)
    profile_data = data.get("profile", {})
    profile = UserProfile(
        full_name=os.getenv("APPLY_FULL_NAME", profile_data.get("full_name", "")),
        first_name=os.getenv("APPLY_FIRST_NAME", profile_data.get("first_name", "")),
        last_name=os.getenv("APPLY_LAST_NAME", profile_data.get("last_name", "")),
        email=os.getenv("APPLY_EMAIL", profile_data.get("email", "")),
        phone=os.getenv("APPLY_PHONE", profile_data.get("phone", "")),
        compensation_range=os.getenv(
            "APPLY_COMP_RANGE", profile_data.get("compensation_range", "")
        ),
        years_experience=os.getenv(
            "APPLY_YEARS", profile_data.get("years_experience", "")
        ),
        industries_supported=profile_data.get("industries_supported", ""),
        organization_size=profile_data.get("organization_size", ""),
        team_size=profile_data.get("team_size", ""),
        degree_response=profile_data.get("degree_response", ""),
        eeo=profile_data.get("eeo", {}),
    )
    return AppConfig(
        cdp_url=data.get("cdp_url", "http://localhost:9223"),
        db_path=Path(data.get("db_path", "jobs.db")),
        answers_path=Path(data.get("answers_path", "answers.json")),
        artifact_dir=Path(data.get("artifact_dir", "artifacts")),
        max_steps=int(data.get("max_steps", 30)),
        captcha_wait_seconds=int(data.get("captcha_wait_seconds", 120)),
        profile=profile,
    )
