"""The private Chromium user-data directory for local Meet presentation mode."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from hermes_constants import get_hermes_home


def persistent_chrome_enabled() -> bool:
    from hermes_cli.config import load_config_readonly

    settings = load_config_readonly().get("plugins", {}).get("entries", {}).get("google_meet", {})
    return isinstance(settings, dict) and settings.get("persistent_chrome") is True


def chrome_profile_path() -> Path:
    return Path(get_hermes_home()) / "workspace" / "meetings" / "chrome-profile"


@contextmanager
def meet_chrome_profile():
    """Give either the auth helper or the meeting exclusive use of the same profile."""
    import fcntl

    path = chrome_profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir(mode=0o700, exist_ok=True)
    if path.is_symlink() or path.stat().st_uid != os.geteuid():
        raise RuntimeError("The Meet Chrome profile must be a private directory owned by Hermes.")
    path.chmod(0o700)
    with (path / ".hermes.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("The Meet Chrome profile is already open. Close its sign-in window or stop the current Meet bot first.") from None
        yield path


def authorize_meet_chrome() -> int:
    """Sign in through the presentation browser, and flush the profile on exit."""
    from hermes_cli.browser_runtime import chromium_executable
    from playwright.sync_api import sync_playwright
    from plugins.google_meet.presentation import PresentationError, bot_screen_env

    try:
        env = bot_screen_env(dict(os.environ))
        with meet_chrome_profile() as path, sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(
                path, channel="chromium", executable_path=chromium_executable(),
                headless=False, env=env, ignore_default_args=["--disable-infobars"],
            )
            try:
                page = context.pages[0]
                page.goto("https://myaccount.google.com/", wait_until="domcontentloaded", timeout=30_000)
                while True:
                    input("Sign in to Google in the Meet Chrome window, then press Enter to check it. ")
                    response = page.goto("https://myaccount.google.com/", wait_until="domcontentloaded", timeout=30_000)
                    cookies = context.cookies(["https://myaccount.google.com/"])
                    if (response and response.ok and urlsplit(page.url).hostname == "myaccount.google.com"
                            and any(cookie["name"] == "SID" for cookie in cookies)):
                        break
                    print("Google still requires sign-in. Finish it in the same window and try again.")
            finally:
                context.close()
        print("Google sign-in verified and saved in the persistent Meet Chrome profile.")
        return 0
    except (RuntimeError, PresentationError, EOFError) as exc:
        print(str(exc) or "The Google sign-in check was cancelled.")
    except Exception as exc:
        print(f"Google sign-in check failed ({type(exc).__name__}).")
    return 1
