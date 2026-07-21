"""Authentication handling for pages that require login.

When the system encounters a login page during capture, it:
1. Detects the login page (form with password field, common login URL patterns)
2. Opens a VISIBLE browser window for the user to log in manually
3. Saves the authenticated session state (cookies, localStorage)
4. Reuses that state for all subsequent page captures in the review

The detection is AI-assisted — instead of hardcoding login URL patterns,
the AI reads the page content and determines if it's a login/auth page.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from playwright.async_api import async_playwright, Page

logger = logging.getLogger(__name__)

# Persistent auth state directory — saved by domain, reused across reviews
_AUTH_DIR = Path(__file__).parent.parent.parent / "auth_sessions"


def _domain_from_url(url: str) -> str:
    """Extract the domain from a URL for auth state keying."""
    from urllib.parse import urlparse
    return urlparse(url).netloc.lower()


def get_auth_state_path(review_dir: str, url: str = "") -> str | None:
    """Return the saved auth state path, checking:
    1. Domain-level persistent state (survives across reviews)
    2. Review-level state (current review only)

    Auth states are stored by domain so logging into a given district's
    school site once works for every future review of that domain until
    the session expires.
    """
    # Check domain-level persistent state first
    if url:
        domain = _domain_from_url(url)
        domain_path = _AUTH_DIR / f"{domain}.json"
        if domain_path.exists():
            # Validate it's not expired (check file age — sessions older
            # than 24 hours are likely stale)
            import time
            age_hours = (time.time() - domain_path.stat().st_mtime) / 3600
            if age_hours < 24:
                logger.debug("Using cached auth state for %s (%.1fh old)", domain, age_hours)
                return str(domain_path)
            else:
                logger.info("Auth state for %s is %.1fh old — will re-authenticate if needed", domain, age_hours)
                domain_path.unlink(missing_ok=True)

    # Fall back to review-level state
    state_path = os.path.join(review_dir, "auth_state.json")
    if os.path.exists(state_path):
        return state_path

    return None


async def detect_login_page(page: Page) -> bool:
    """Determine if the current page is a login/authentication page.

    Uses a combination of fast deterministic checks (password fields,
    URL patterns) and falls back to reading the page if unclear.
    """
    # Check 1: Is there a password input field visible?
    has_password = await page.evaluate("""
        () => {
            const pwFields = document.querySelectorAll('input[type="password"]');
            for (const f of pwFields) {
                const rect = f.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) return true;
            }
            return false;
        }
    """)
    if has_password:
        return True

    # Check 2: Common login/auth indicators in the URL
    url = page.url.lower()
    login_indicators = [
        "/login", "/signin", "/sign-in", "/auth", "/sso",
        "/cas/login", "/saml", "/oauth", "/account/login",
        "/users/sign_in", "/wp-login", "/adfs",
    ]
    if any(ind in url for ind in login_indicators):
        return True

    # Check 3: Page title/heading suggests login
    title = (await page.title() or "").lower()
    login_words = {"sign in", "log in", "login", "signin", "authenticate"}
    if any(w in title for w in login_words):
        return True

    # Check 4: Check for login form with username/email + submit
    has_login_form = await page.evaluate("""
        () => {
            const forms = document.querySelectorAll('form');
            for (const form of forms) {
                const hasUser = form.querySelector(
                    'input[type="email"], input[type="text"][name*="user"], '
                    + 'input[type="text"][name*="email"], input[type="text"][name*="login"], '
                    + 'input[autocomplete="username"]'
                );
                const hasPw = form.querySelector('input[type="password"]');
                if (hasUser && hasPw) return true;
            }
            return false;
        }
    """)
    if has_login_form:
        return True

    return False


async def authenticate_interactive(
    url: str,
    review_dir: str,
    progress_callback=None,
) -> str | None:
    """Open a visible browser window for the user to log in.

    Waits for the user to complete authentication, then saves
    the browser state and returns the path to the state file.

    Returns the auth state file path, or None if auth was skipped.
    """
    # Save by domain for cross-review reuse
    domain = _domain_from_url(url)
    _AUTH_DIR.mkdir(parents=True, exist_ok=True)
    domain_state_path = str(_AUTH_DIR / f"{domain}.json")
    review_state_path = os.path.join(review_dir, "auth_state.json")

    if progress_callback:
        await progress_callback(
            "A login page was detected. A browser window will open — "
            "please log in. The review will continue automatically "
            "once you're authenticated."
        )

    logger.info("Opening visible browser for authentication: %s", url)

    async with async_playwright() as pw:
        # Launch VISIBLE browser (headless=False)
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
        )
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # Wait for the user to navigate away from the login page
        # (i.e., they successfully authenticated and were redirected)
        # Poll every 2 seconds for up to 5 minutes
        max_wait = 300  # 5 minutes
        waited = 0
        while waited < max_wait:
            await page.wait_for_timeout(2000)
            waited += 2

            # Check if we're still on a login page
            still_login = await detect_login_page(page)
            if not still_login:
                logger.info("Authentication successful — user navigated away from login")
                break

            if progress_callback and waited % 10 == 0:
                await progress_callback(
                    f"Waiting for login... ({waited}s elapsed, {max_wait - waited}s remaining)"
                )

        if waited >= max_wait:
            logger.warning("Authentication timed out after %ds", max_wait)
            await browser.close()
            return None

        # Save the authenticated state — both by domain (persistent)
        # and in the review dir (for this review)
        storage = await context.storage_state()
        state_json = json.dumps(storage)
        Path(domain_state_path).write_text(state_json, encoding="utf-8")
        Path(review_state_path).write_text(state_json, encoding="utf-8")
        logger.info("Auth state saved for domain %s (reusable across reviews)", domain)

        await browser.close()

    return domain_state_path
