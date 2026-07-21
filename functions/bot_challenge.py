"""Bot-protection interstitial detection.

Cloudflare / hCaptcha / reCAPTCHA challenge pages are served INSTEAD of
the requested page when a WAF decides the browser is a bot. Auditing the
interstitial produces a well-formed but meaningless ACR (verified: a run
against a Cloudflare-fronted site earnestly reported the challenge
widget's keyboard trap and the interstitial's meta refresh). Capture
must detect the interstitial and fail loudly so the operator knows the
target was never reached.

Detection is a cheap deterministic check (title + body markers on a
near-empty DOM), which is the correct tool here: the signals are exact
vendor strings, not judgment calls.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_TITLE_MARKERS = (
    "just a moment",
    "attention required",
    "verifying you are human",
    "checking your browser",
    "access denied",
    "security check",
)

_BODY_MARKERS = (
    "verify you are human",
    "checking your browser before accessing",
    "performing security verification",
    "protect against malicious bots",
    "cf-challenge",
    "cf-turnstile",
    "challenge-platform",
    "hcaptcha.com/captcha",
    "g-recaptcha",
    "ddos protection by",
)


async def detect_bot_challenge(page) -> str | None:
    """Return a human-readable description when *page* looks like a
    bot-protection interstitial, else None. Never raises."""
    try:
        title = (await page.title() or "").lower()
        for marker in _TITLE_MARKERS:
            if marker in title:
                return f"page title matches bot-challenge marker '{marker}'"
        # Body markers only count on a near-empty page: a real article
        # ABOUT Cloudflare must not trip the detector.
        body = await page.evaluate(
            "() => (document.body ? document.body.innerText.slice(0, 5000) : '')"
        )
        content = (body or "").lower()
        html = await page.content()
        if len(content.strip()) < 1500:
            for marker in _BODY_MARKERS:
                if marker in content or marker in html.lower():
                    return f"bot-challenge marker '{marker}' on a near-empty page"
        return None
    except Exception as exc:
        logger.warning("Bot-challenge probe failed (treating as no challenge): %s", exc)
        return None


async def wait_out_bot_challenge(page, *, max_wait_s: float = 25.0) -> str | None:
    """Give an auto-clearing challenge ('checking your browser…') time to
    pass, re-probing every few seconds.

    Returns None when the page is (or becomes) the real content, or the
    challenge description when it persists past ``max_wait_s`` — the
    caller should then abort capture with a loud error instead of
    auditing the interstitial.
    """
    detected = await detect_bot_challenge(page)
    if not detected:
        return None
    logger.warning(
        "Bot-protection challenge detected (%s) -- waiting up to %.0fs for auto-clearance",
        detected, max_wait_s,
    )
    waited = 0.0
    step = 5.0
    while waited < max_wait_s:
        await asyncio.sleep(step)
        waited += step
        detected = await detect_bot_challenge(page)
        if not detected:
            logger.info("Bot-protection challenge cleared after %.0fs", waited)
            return None
    return detected
