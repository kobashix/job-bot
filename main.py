"""Entry point for async job application automation."""
from __future__ import annotations

import asyncio
import os
import sys

os.environ.setdefault("NODE_OPTIONS", "--no-deprecation")

from core.session import InvalidJobPage, NavigationTimeout, PlaywrightSession, SessionExit
from helpers.captcha import CaptchaConfig, CaptchaHandler
from helpers.db import DBClient
from helpers.form_fill import AnswersStore, FormFiller
from helpers.utils import parse_args, load_config, setup_logging, ContextLogger


CAPTCHA_IFRAME_SELECTORS = [
    "iframe[src*='captcha' i]",
    "iframe[src*='recaptcha' i]",
    "iframe[src*='hcaptcha' i]",
    "iframe[src*='api2/anchor' i]",
    "iframe[src*='api2/bframe' i]",
    "iframe[title*='captcha' i]",
    "iframe[title*='recaptcha' i]",
    "iframe[title*='hcaptcha' i]",
    "div[aria-label*='captcha' i]",
    "textarea[name='g-recaptcha-response' i]",
    "textarea[name='h-captcha-response' i]",
    "input[name='g-recaptcha-response' i]",
    "input[name='h-captcha-response' i]",
    ".grecaptcha-badge",
    ".h-captcha",
    "div[id*='recaptcha' i]",
    "div[class*='recaptcha' i]",
]

CAPTCHA_TEXT_LOCATOR = "text=/i'm not a robot|verify you are human|security check|captcha/i"
CAPTCHA_CHALLENGE_LOCATOR = "iframe[title*='challenge' i], iframe[src*='challenge' i]"
BLOCK_TEXTS = [
    "i'm not a robot",
    "verify you are human",
    "captcha",
    "security check",
]


async def run() -> int:
    args = parse_args()
    config = load_config(args.config)
    logger = setup_logging()
    
    if args.job_url:
        return await run_single(args.job_url, args.train, config, logger)
    else:
        # Default to batch mode if no URL is provided, using a default limit
        limit = int(os.environ.get("APPLY_LIMIT", 10))
        return await run_batch(limit, args.train, config, logger)

async def run_single(job_url: str, train: bool, config, logger) -> int:
    context_logger = ContextLogger(logger, {"job_url": job_url, "ats": "indeed", "step": "init"})
    db_client = DBClient(str(config.db_path), context_logger)
    answers = AnswersStore.load(config.answers_path)
    form_filler = FormFiller(config.profile, answers, context_logger, train)

    captcha_config = CaptchaConfig(
        iframe_selectors=CAPTCHA_IFRAME_SELECTORS,
        text_locator=CAPTCHA_TEXT_LOCATOR,
        challenge_locator=CAPTCHA_CHALLENGE_LOCATOR,
        block_texts=BLOCK_TEXTS,
    )
    captcha_handler = CaptchaHandler(captcha_config, context_logger, config.captcha_wait_seconds)

    session = PlaywrightSession(
        job_url=job_url,
        train_mode=train,
        config=config,
        db=db_client,
        captcha=captcha_handler,
        form_filler=form_filler,
        logger=context_logger,
    )

    try:
        await session.run()
    except SessionExit as exc:
        return exc.code
    except NavigationTimeout:
        return 11
    except InvalidJobPage:
        return 12
    except Exception as exc:
        context_logger.error("Unhandled exception: %s", exc)
        await db_client.update_job(job_url, "ERROR_unhandled_exception", f"unhandled_exception:{exc}")
        return 98
    return 0

async def run_batch(limit: int, train: bool, config, logger) -> int:
    context_logger = ContextLogger(logger, {"job_url": "batch", "ats": "batch", "step": "init"})
    db_client = DBClient(str(config.db_path), context_logger)
    
    # Exclude terminal statuses
    excluded = ("APPLIED", "EXTERNAL_APPLY", "NOT_REMOTE", "EXPIRED", "CAPTCHA_BLOCKED")
    jobs = await db_client.fetch_jobs(limit, excluded)
    
    if not jobs:
        context_logger.info("No jobs to process in batch")
        return 0

    context_logger.info("Starting batch of %s jobs", len(jobs))
    success_count = 0
    for idx, (url,) in enumerate(jobs, 1):
        context_logger.info("[%s/%s] processing: %s", idx, len(jobs), url)
        code = await run_single(url, train, config, logger)
        if code == 0:
            success_count += 1
        # Optional: break on specific error codes if needed (e.g. captcha limit)
        if code == 21: # verification_prompt_failed
            context_logger.warning("Stopping batch due to manual verification required")
            break
            
    context_logger.info("Batch complete. Successes: %s/%s", success_count, len(jobs))
    return 0


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
